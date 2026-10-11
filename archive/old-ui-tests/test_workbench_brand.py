"""外壳的品牌与图表。

业主的要求是"界面用 logo 替代文件名"，这条听起来是审美，实际是两个判据：
1. 标题栏/侧栏不该出现目录名 —— 那是把"一个文件夹"当成产品；
2. 新加的图必须只画真从队列库里数出来的数，未知状态不许被丢掉
   （丢掉的段会让人以为"全绿"，而它只是不认识那几种状态）。
"""
from __future__ import annotations

import re

from tools import workbench_ui as ui


def _page() -> str:
    return ui.page("任务", "tasks", ["<div>x</div>"])


class TestBrandIsALogo:
    def test_the_mark_is_inline_svg(self):
        page = _page()
        assert "<svg class='mark'" in page
        assert "aria-label='MAO'" in page

    def test_no_image_file_is_requested(self):
        """内联 = 不多一个静态资源路由，也不引入依赖；侧栏与 favicon 用同一份图。"""
        page = _page()
        assert ".png" not in page and ".ico" not in page
        assert page.count("<svg class='mark'") == 1          # 侧栏那份
        href = page.split("rel='icon' href='")[1].split("'")[0]
        assert href.startswith("data:image/svg+xml,")
        assert "%232563eb" in href                            # 同一个标记，# 已转义
        assert "'" not in href, "引号没转义会把 href 截断成半个 SVG"

    def test_the_folder_name_is_not_used_as_the_brand(self):
        page = _page()
        assert "multi-agent-orchestrator" not in page
        assert "Multi-Agent Orchestrator" not in page
        assert "MAO 工作台" in page

    def test_three_nodes_for_three_roles(self):
        """图不是装饰：三个节点就是验收/执行/评审，颜色与侧栏角色格一致。"""
        circles = re.findall(r"<circle [^>]*fill='(#[0-9a-f]{6})'",
                             ui.LOGO_SVG)
        assert len(circles) == 3
        assert circles[0] == "#2563eb"            # 验收（与 .on 的强调色同源）
        assert circles[1] == "#0f766e"            # 执行
        assert circles[2] == "#b45309"            # 评审


class TestQueueChart:
    def test_empty_queue_says_so_instead_of_drawing_a_zero(self):
        assert "队列是空的" in ui._queue_chart({})

    def test_every_nonzero_status_appears_with_its_real_number(self):
        counts = {"RUNNING": 2, "QUEUED": 5, "COMPLETED": 3}
        html = ui._queue_chart(counts)
        for status, n in counts.items():
            assert f"{status} {n}" in html
        assert abs(sum(float(m) for m in re.findall(r"width:([\d.]+)%", html))
                   - 100.0) < 0.05

    def test_unknown_status_is_not_silently_dropped(self):
        """这一段最容易骗人：不认识的键被 filter 掉，图就永远好看。"""
        html = ui._queue_chart({"RUNNING": 1, "TELEPORTING": 4})
        assert "TELEPORTING 4" in html
        assert html.count("<i style=") == 2

    def test_zero_counts_do_not_take_up_space_or_lie(self):
        html = ui._queue_chart({"FAILED": 0, "RUNNING": 1})
        assert "FAILED" not in html and "RUNNING 1" in html
