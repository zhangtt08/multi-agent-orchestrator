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
    def __init__(self, running):
        self.running = running


def _ctx(running=False):
    return SimpleNamespace(config_dir="config", runner=Runner(running),
                           real_roles=True, default_strategy="COPY",
                           last_go={}, last_form={}, last_plan={})


def line(running, rows):
    return ui.agent_activity_line(_ctx(running), rows)


class TestAgentActivityLine:
    def test_idle_scheduler_is_called_idle(self):
        """这是业主踩到的那一格：调度器活着 ≠ 有 agent 在跑。"""
        text = line(True, [])
        assert "在跑但队列是空的" in text and "没有任何" in text and "空转" in text
        assert "class='note bad'" in text

    def test_a_running_task_is_named_with_its_stage(self):
        text = line(True, [{"runtime_task_id": "rt-abc123456789",
                            "status": "RUNNING", "stage": "EXECUTING"}])
        assert "有 agent 在跑" in text and "rt-abc12345" in text and "EXECUTING" in text

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
        assert "2 条" in line(True, [{"runtime_task_id": "a", "status": "RUNNING"},
                                     {"runtime_task_id": "b", "status": "RUNNING"}])


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
