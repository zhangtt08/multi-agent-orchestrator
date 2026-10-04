"""任务页上那两句"到底有没有东西在跑"的实话，以及切分之后的开工门。

业主实跑后的原话：「输入任务，自行切分后，并没有看到 agent 是否运行，
调度器运行中但一直未进行交付」。查下来是两件事叠在一起：

1. `/go` 在入队那一步抛 BatchError（落地目录不是 git 仓库），整条连接被
   socketserver 打断 —— 页面上什么都没显示，人只看到"调度器运行中"；
2. 切分产物 `runtime_batch/planned/*.project.json` 在界面上**只有一条命令**作为
   下一步，等于把"输入 → 切分 → 开工"这条链在中间断开。

所以这里锁的是措辞与形状：空转必须被叫成空转，切好的清单必须能在页上开工。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tools import workbench_ui as ui


class Runner:
    """替身按**真类的形状**给：`SchedulerRunner.running` 是方法不是属性。

    以前这里是 `self.running = running`（一个布尔属性），于是
    `getattr(ctx.runner, "running", False)` 在生产里拿到的是恒真的 bound method、
    在测试里拿到的是真布尔 —— 矩阵全绿而页面在撒谎（AGENTS.md 地雷 30 的原话）。
    断言一个字没动，动的是夹具与真接口的形状差。
    """

    def __init__(self, running):
        self._running = running

    def running(self):
        return self._running


def _ctx(running=False):
    return SimpleNamespace(config_dir="config", runner=Runner(running),
                           real_roles=True, default_strategy="COPY",
                           last_go={}, last_form={}, last_plan={})


def line(running, rows):
    return ui.agent_activity_line(_ctx(running), rows)


class TestAgentActivityLine:
    """这一族的行**按真读层的形状**给：`dv.snapshot()` 每行都带 `lease_state`。

    地雷 30 的课就是"替身形状与真接口不一样，于是矩阵全绿而生产在撒谎"。
    "有没有 agent 在跑"这一句的判据是租约（地雷 42），所以替身必须带租约结论，
    否则用例只能在状态字上过关，而那一格恰恰是它说错的地方。
    """

    def test_idle_scheduler_is_called_idle(self):
        """这是业主踩到的那一格：调度器活着 ≠ 有 agent 在跑。"""
        text = line(True, [])
        assert "在跑但队列是空的" in text and "没有任何" in text and "空转" in text
        assert "class='note bad'" in text

    def test_a_running_task_is_named_with_its_stage(self):
        text = line(True, [{"runtime_task_id": "rt-abc123456789",
                            "status": "RUNNING", "stage": "EXECUTING",
                            "lease_state": ui.dv.LEASE_HELD,
                            "lease_worker": "w1", "lease_expires": "2099-01-01T00:00:00+00:00"}])
        assert "有 agent 在跑" in text and "rt-abc12345" in text and "EXECUTING" in text
        assert "租约仍被持有" in text

    def test_a_running_row_with_an_expired_lease_is_not_called_running(self):
        """地雷 42 的形状：状态字 RUNNING + 过期租约 = 干活的人已经死了。

        这一条以前是红的方向相反 —— 页面会说"有 agent 在跑"，而业主看到的是
        "跑到一半没了动静"。不许再把两者混起来。
        """
        for state in (ui.dv.LEASE_STALE, ui.dv.LEASE_ABSENT):
            text = line(False, [{"runtime_task_id": "rt-deadbeef0000",
                                 "status": "RUNNING", "stage": "EXECUTING",
                                 "lease_state": state, "lease_worker": "w1",
                                 "lease_expires": "2020-01-01T00:00:00+00:00"}])
            assert "没有 agent 在跑" in text, (state, text)
            assert "现在<b>有 agent 在跑</b>" not in text, (state, text)
            assert "接管" in text and "重新提交" in text, (state, text)

    def test_a_live_lease_and_an_abandoned_one_are_reported_separately(self):
        """两堆不许并成一句" —— 一条真在跑、一条是崩溃留下的现场。"""
        text = line(True, [
            {"runtime_task_id": "rt-live00000001", "status": "RUNNING",
             "stage": "EXECUTING", "lease_state": ui.dv.LEASE_HELD},
            {"runtime_task_id": "rt-dead00000002", "status": "RUNNING",
             "stage": "EXECUTING", "lease_state": ui.dv.LEASE_STALE}])
        assert "没有 agent 在跑" in text and "仍被持有" in text

    def test_no_lease_table_says_no_record_instead_of_guessing(self):
        """读不出租约就说"没有记录" —— 既不能说在跑，也不能说没在跑。"""
        text = line(True, [{"runtime_task_id": "rt-unknown00001",
                            "status": "RUNNING", "stage": "EXECUTING"}])
        assert "没有记录" in text and "现在<b>有 agent 在跑</b>" not in text

    def test_pending_without_scheduler_says_what_to_press(self):
        text = line(False, [{"runtime_task_id": "rt-1", "status": "QUEUED"}])
        assert "调度器没启动" in text and "启动调度器" in text

    def test_running_scheduler_with_pending_work_is_not_called_idle(self):
        """这一格最容易被写成"在跑"就完事 —— 但队列里有条目却没开始，是另一回事。"""
        text = line(True, [{"runtime_task_id": "rt-1", "status": "QUEUED"}])
        assert "待领" in text and "容量" in text and "空转" not in text

    def test_empty_everything_says_so(self):
        assert "队列是空的" in line(False, [])

    def test_no_invented_numbers(self):
        """条数必须来自传进来的行，不许是写死的。"""
        assert "2 条" in line(True, [
            {"runtime_task_id": "a", "status": "RUNNING",
             "lease_state": ui.dv.LEASE_HELD},
            {"runtime_task_id": "b", "status": "RUNNING",
             "lease_state": ui.dv.LEASE_HELD}])

    def test_the_real_runner_class_is_the_sentinel_not_the_double(self):
        """用**真** SchedulerRunner（没 start 过）问一次，必须是"调度器没启动"那一支。

        替身给的是形状，真类给的才是判据：bound method 恒真那一次（地雷 30）
        矩阵全绿、生产里调度器从来没起来过。这条哨兵拿的是产品自己那个类，
        把 `.running` 当属性读就当场红。
        """
        from tools.scheduler_cli import SchedulerRunner

        ctx = SimpleNamespace(config_dir="config",
                              runner=SchedulerRunner("config"),
                              real_roles=True, default_strategy="COPY",
                              last_go={}, last_form={}, last_plan={})
        text = ui.agent_activity_line(
            ctx, [{"runtime_task_id": "rt-1", "status": "QUEUED"}])
        assert "调度器没启动" in text, text
        assert "在跑" not in text, text


class TestTheQueueRowThatNobodyHolds:
    """哨兵：拿**真的** TaskRepository + 真的读层（`dv.queue_rows/snapshot`）走一遍。

    上面那些用例测的是替身形状；这一族测的是"崩溃之后那一格在页面上被说成什么"。
    判据链条必须一路对上：`task_leases` 表里的真实过期时刻 →
    `clock.lease_is_stale` → `TaskRepository.lease_expired()`/`stale_running()`
    → `delivery_view.lease_state_of()` → 页面上那一句。
    任何一环各写一遍自己的判断，这里就会红（地雷 42 说的那件事）。
    """

    def _seed(self, tmp_path, monkeypatch, *, lease_seconds: float):
        """真类现场：真 TaskRepository + 真 TaskSubmissionService 领用一格。

        时间用 SystemClock —— 读层（`delivery_view`）问的就是这台机器的现在，
        夹具再拿 FakeClock 造"还剩 50 秒"会让两边对不上（那不是被测性质）。
        过期那一格用 `lease_seconds=-1` 直接写出一个已经过去的到期时刻，
        不需要 time.sleep（§57：时间推进不许靠等）。
        """
        from mao.core.models import Task
        from mao.scheduler import TaskRepository, TaskSubmissionService
        from mao.scheduler.clock import SystemClock

        clock = SystemClock()
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "settings.yaml").write_text(
            "runtime_dir: attempts\nscheduler:\n"
            "  db_path: queue.db\n  attempts_root: attempts\n", encoding="utf-8")
        monkeypatch.setattr(ui.dv, "ROOT", tmp_path)
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        service = TaskSubmissionService(repo, clock=clock)
        rt = service.submit(Task(goal="做一份首页", context={"task_type": "demo"},
                                 workspace_path="w://x"))
        lease = repo.try_acquire_next("w1", lease_seconds=lease_seconds,
                                      max_concurrent=1)
        assert lease is not None, "前提：真类领用这一格失败"
        assert str(repo.get(rt.runtime_task_id).status.value) == "RUNNING"
        repo.close()
        return rt.runtime_task_id

    def _ctx(self):
        from tools.scheduler_cli import SchedulerRunner

        return SimpleNamespace(config_dir="config",
                               runner=SchedulerRunner("config"),
                               real_roles=True, default_strategy="COPY",
                               last_go={}, last_form={}, last_plan={})

    def test_a_held_lease_is_reported_as_running_by_the_real_reader(self, tmp_path,
                                                                    monkeypatch):
        rt = self._seed(tmp_path, monkeypatch, lease_seconds=3600)
        rows = ui.dv.queue_rows("config", limit=50)
        assert [r["lease_state"] for r in rows] == [ui.dv.LEASE_HELD], rows
        snap = ui.dv.snapshot("config", rows[0])
        assert snap["lease_state"] == ui.dv.LEASE_HELD
        text = ui.agent_activity_line(self._ctx(), [snap])
        assert "现在<b>有 agent 在跑</b>" in text and rt[:12] in text
        assert "没有 agent 在跑" not in text

    def test_an_expired_lease_is_reported_as_nobody_running(self, tmp_path,
                                                             monkeypatch):
        rt = self._seed(tmp_path, monkeypatch, lease_seconds=-1)
        # 真调度器自己那两份判据先对齐（同一个 clock.lease_is_stale）
        from mao.scheduler import TaskRepository
        from mao.scheduler.clock import SystemClock

        live = TaskRepository(tmp_path / "queue.db", clock=SystemClock())
        try:
            assert live.lease_expired(rt) is True
            assert [r["runtime_task_id"] for r in live.stale_running()] == [rt]
        finally:
            live.close()

        rows = ui.dv.queue_rows("config", limit=50)
        assert [r["lease_state"] for r in rows] == [ui.dv.LEASE_STALE], rows
        snap = ui.dv.snapshot("config", rows[0])
        text = ui.agent_activity_line(self._ctx(), [snap])
        assert "没有 agent 在跑" in text, text
        assert "现在<b>有 agent 在跑</b>" not in text, text
        assert "接管" in text and "重新提交" in text
        # 队列那一列的"为什么停在这儿"也不能再说它"在跑"
        assert "没人真的在跑" in ui._stop_reason(snap, "config")

    def test_the_dashboard_note_counts_the_two_kinds_separately(self, tmp_path,
                                                                monkeypatch):
        self._seed(tmp_path, monkeypatch, lease_seconds=-1)
        note = ui.stale_running_note("config")
        assert "1 条没人持有租约" in note, note
        assert "没有 agent 在跑" in note and "COMMITTED 恢复点" in note
        assert "进行中" not in note           # 这一行不重算 KPI，只解释那一列


class TestPlannedCardCanStart:
    def _planned(self, tmp_path, monkeypatch):
        root = tmp_path / "runtime_batch" / "planned"
        root.mkdir(parents=True)
        spec = {"name": "ztt", "workspace": "C:/somewhere", "strategy": "COPY",
                "milestones": [{"id": "m1-one", "goal": "写验收脚本",
                                "acceptance": "pytest -q"}]}
        (root / "ztt.project.json").write_text(json.dumps(spec, ensure_ascii=False),
                                               encoding="utf-8")
        monkeypatch.setattr(ui, "ROOTISH", tmp_path)

    def test_the_card_offers_a_button_not_only_a_command(self, tmp_path, monkeypatch):
        self._planned(tmp_path, monkeypatch)
        page = "".join(ui.planned_section())
        assert "action='/start-plan'" in page
        assert "按这张清单开工" in page
        assert "name='project'" in page            # 项目档路径随表单带走，不让人抄

    def test_the_command_line_equivalent_is_still_shown(self, tmp_path, monkeypatch):
        """按钮不是要藏掉 CLI —— 两条路都要能走，人才信这一格没骗他。"""
        self._planned(tmp_path, monkeypatch)
        page = "".join(ui.planned_section())
        assert "batch_project.py ship" in page
        # v1.8：默认无人值守，人工门改成一个能看见的开关，而不是藏起来
        assert '"mode": "human"' in page and "合入仍要你说一句" not in page
