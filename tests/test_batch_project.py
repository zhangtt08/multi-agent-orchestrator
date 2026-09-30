"""批次层的守卫测试。

重点不是"能不能提交"，是三条承诺：一次只推进一格、没合入就不许前进、
以及这个工具**没有能力**替你 merge。
"""
from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

from tools import batch_project as bp


def write_spec(tmp_path, **over):
    spec = {"name": "t", "workspace": str(tmp_path), "strategy": "COPY",
            "milestones": [{"id": "m1", "goal": "把 multiply 改成返回 a * b",
                            "acceptance": "pytest test_calc.py -q"}]}
    spec.update(over)
    p = tmp_path / "project.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return p


def git(tmp_path, *args):
    return subprocess.run(["git", *args], cwd=str(tmp_path), check=True,
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _git_sees_only_this_repo_config(monkeypatch, tmp_path):
    """让这一格不靠"本机恰好有没有全局 git 身份"过关（地雷 38 的反面）。

    上一台机器没有全局身份，所以 `accept` 走的是"基线署名就是工作台的兜底身份 ->
    沿用同一个身份"那一支；这台机器有全局身份，同一份代码、同一条判据却改了结论。
    被测的是判据，不是那台机器的 `git config` —— 所以把 global/system 两份配置
    指到空文件，仓库自己那一份就是唯一变量。
    """
    empty = tmp_path / "gitconfig-isolated"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """一个真 git 仓库当 workspace —— 带身份配置，因为 accept 要提交。"""
    git(tmp_path, "init", "-q", ".")
    git(tmp_path, "config", "user.name", "tester")
    git(tmp_path, "config", "user.email", "tester@example.com")
    (tmp_path / "calc.py").write_text("def multiply(a, b):\n    return a + b\n",
                                      encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path / "runtime_batch")
    return tmp_path


class TestSpecValidation:
    def test_each_problem_is_one_actionable_sentence(self, tmp_path):
        cases = [
            ({"name": ""}, "name"),
            ({"workspace": ""}, "workspace"),
            ({"milestones": []}, "milestones"),
            ({"workspace": str(tmp_path / "没这个目录")}, "不存在"),
        ]
        for over, needle in cases:
            p = write_spec(tmp_path, **over)
            with pytest.raises(bp.BatchError) as exc:
                bp.load_spec(p)
            assert needle in str(exc.value), (over, str(exc.value))

    def test_milestone_without_acceptance_is_refused(self, tmp_path):
        p = write_spec(tmp_path, milestones=[
            {"id": "m1", "goal": "把 multiply 改成返回 a * b，并让测试通过"}])
        with pytest.raises(bp.BatchError, match="acceptance"):
            bp.load_spec(p)

    def test_a_command_with_a_chinese_filename_is_a_command(self, tmp_path):
        """判据是"像不像命令"，不是"是不是 ASCII"。

        业主 2026-09-30 的输入是「创建一个1111文档」—— 这条验收命令
        `test -f 1111文档.md` 完全合格，旧的整条 ASCII 判据会把它当描述拒掉，
        于是"怎么填都不行"。中文文件名在这台机器上是常态。
        """
        p = write_spec(tmp_path, milestones=[
            {"id": "m1", "goal": "在工作区根新建 1111文档.md，写清标题与用途",
             "acceptance": "test -f 1111文档.md"}])
        spec = bp.load_spec(p)
        assert spec["milestones"][0]["acceptance"] == "test -f 1111文档.md"

    def test_a_sentence_is_still_refused_and_says_which_shape_rule_it_broke(
            self, tmp_path):
        for acc, needle in (("首页能打开就行", "开头不是可执行名"),
                            ("验收：文件存在", "中文标点"),
                            ("pytest -q\npytest -r", "跨行")):
            p = write_spec(tmp_path, milestones=[
                {"id": "m1", "goal": "在工作区根新建 index.html 并写好标题",
                 "acceptance": acc}])
            with pytest.raises(bp.BatchError) as exc:
                bp.load_spec(p)
            assert needle in str(exc.value), (acc, str(exc.value))

    def test_duplicate_ids_and_unknown_keys_are_named(self, tmp_path):
        p = write_spec(tmp_path, milestones=[
            {"id": "m1", "goal": "把 multiply 改成返回 a * b", "acceptance": "x"},
            {"id": "m1", "goal": "再改一次 divide 让它抛错", "acceptance": "y"}])
        with pytest.raises(bp.BatchError, match="重复"):
            bp.load_spec(p)
        p2 = write_spec(tmp_path)
        data = json.loads(p2.read_text(encoding="utf-8"))
        data["milestone"] = []                      # 单复数写错，很常见
        p2.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(bp.BatchError, match="无法识别的键"):
            bp.load_spec(p2)

    def test_goal_carries_the_acceptance_and_constraints(self, tmp_path):
        spec = bp.load_spec(write_spec(tmp_path, constraints=["不得改 tests"]))
        goal = bp.goal_for(spec, spec["milestones"][0])
        assert "pytest test_calc.py -q" in goal and "退出码 0" in goal
        assert "不得改 tests" in goal


class TestOneStepAtATime:
    def test_awaiting_merge_blocks_the_next_milestone(self, repo):
        spec = bp.load_spec(write_spec(
            repo, milestones=[
                {"id": "m1", "goal": "把 multiply 改成返回 a * b",
                 "acceptance": "pytest -q"},
                {"id": "m2", "goal": "给 divide 补一个除零测试",
                 "acceptance": "pytest -q"}]))
        state = bp.load_state(spec)
        target, _ = bp.next_step(spec, state)
        assert target["id"] == "m1"

        bp.milestone_state(state, "m1")["status"] = "awaiting-merge"
        target, why = bp.next_step(spec, state)
        assert target["id"] == "m1", "上一条还没合入就跳到下一条"
        assert "先 accept" in why

    def test_advance_refuses_when_the_source_head_has_not_moved(self, repo,
                                                                capsys):
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        ms = bp.milestone_state(state, "m1")
        ms.update(status="awaiting-merge", base_before=bp.head(str(repo)))
        assert bp.advance(spec, state) == 2
        assert ms["status"] == "awaiting-merge", "HEAD 没动也算交付了"
        assert "拒绝推进" in capsys.readouterr().out

    def test_advance_accepts_only_after_a_real_commit(self, repo, capsys):
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        before = bp.head(str(repo))
        ms = bp.milestone_state(state, "m1")
        ms.update(status="awaiting-merge", base_before=before)
        (repo / "calc.py").write_text("def multiply(a, b):\n    return a * b\n",
                                      encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@e",
            "commit", "-q", "-m", "milestone: m1")
        assert bp.advance(spec, state) == 0
        assert ms["status"] == "done" and ms["base_after"] != before
        assert "1/1" in capsys.readouterr().out


class TestVerdict:
    def test_partial_batch_is_never_called_done(self, repo):
        spec = bp.load_spec(write_spec(repo, milestones=[
            {"id": "m1", "goal": "把 multiply 改成返回 a * b", "acceptance": "x"},
            {"id": "m2", "goal": "给 divide 补一个除零测试", "acceptance": "y"}]))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "done"
        assert bp.verdict(spec, state) == "未确认"

    def test_all_done_without_a_batch_criterion_says_so(self, repo):
        spec = bp.load_spec(write_spec(repo, final_acceptance=None))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "done"
        text = bp.verdict(spec, state)
        assert "项目完成" not in text and "不给总判定" in text

    def test_verify_records_the_exit_code_and_can_fail(self, repo):
        spec = bp.load_spec(write_spec(
            repo, final_acceptance={"name": "x", "command": [
                "python", "-c", "import sys;sys.exit(3)"]}))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "done"
        assert bp.verify(spec, state) == 1
        assert state["final"]["exit_code"] == 3
        assert bp.verdict(spec, state) == "未确认"

    def test_green_batch_verify_is_the_only_path_to_done(self, repo):
        spec = bp.load_spec(write_spec(
            repo, final_acceptance={"name": "ok", "command": [
                "python", "-c", "import sys;sys.exit(0)"]}))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "done"
        assert bp.verify(spec, state) == 0
        assert bp.verdict(spec, state) == "项目完成"

    def test_unresolvable_command_stays_not_run_not_fail(self, repo):
        """跑不了 ≠ 判红。

        实测：验收命令按 AGENTS.md 写成裸 `pytest`，而从 Git Bash 起的进程 PATH
        里没有它 —— 旧代码在这里抛 FileNotFoundError，既没有判定也没有解释。
        """
        spec = bp.load_spec(write_spec(
            repo, final_acceptance={"name": "nope", "command": [
                "definitely-not-an-installed-executable-9f3c", "-q"]}))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "done"
        lines = []
        assert bp.verify(spec, state, echo=lines.append) == 2
        assert state.get("final", {}).get("status") != "fail", \
            "一次都没跑起来的命令被写成了批次判红"
        assert any("起不来" in ln for ln in lines)
        assert any("not-run" in ln for ln in lines)


class TestMergeNeedsAHuman:
    """合入这条路的形状：只有一扇门，且门上要签名。"""

    def _git_calls_by_function(self):
        """AST：每个函数里出现的 `git <子命令>` 调用点。"""
        import ast

        tree = ast.parse(Path(bp.__file__).read_text(encoding="utf-8"))
        found: dict[str, list[str]] = {}
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            verbs = []
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                fname = getattr(call.func, "attr", "")
                if fname not in ("run", "Popen") or not call.args:
                    continue
                first = call.args[0]
                if (isinstance(first, ast.List) and first.elts
                        and isinstance(first.elts[0], ast.Constant)
                        and first.elts[0].value == "git"
                        and len(first.elts) > 1
                        and isinstance(first.elts[1], ast.Constant)):
                    verbs.append(str(first.elts[1].value))
            if verbs:
                found.setdefault(fn.name, []).extend(verbs)
        return found

    def test_only_accept_touches_the_repo(self):
        calls = self._git_calls_by_function()
        for fn, verbs in calls.items():
            if fn == "accept":
                continue
            assert set(verbs) == {"rev-parse"}, (
                f"{fn} 里出现了改仓库的 git 动词 {verbs} —— 除 accept 之外"
                "任何路径都不许动源仓库")
        assert set(calls.get("accept", [])) & {"apply", "commit"}, \
            "accept 不做 apply/commit 的话，这条路就白建了"

    def test_a_repo_without_a_commit_identity_is_refused_before_anything_moves(
            self, repo, capsys):
        """没有提交身份就整条拒绝 —— 而不是 apply 完再让 commit 炸。

        演练出来的：这台机器没有全局 git 身份，`accept` 以前会先把补丁
        apply 进工作区、再在 commit 上失败，留下一个"改了但没提交"的仓库。
        """
        git(repo, "config", "--local", "--unset", "user.name")
        git(repo, "config", "--local", "--unset", "user.email")
        spec, state, patch = self._ready(repo, tmp_patch=True)
        capsys.readouterr()
        assert bp.accept(spec, bp.load_state(spec), confirmed=True) == 2
        out = capsys.readouterr().out
        assert "没有提交身份" in out and "git -C" in out
        # 工作区没被动过：calc.py 还是基线那行
        assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")

    def test_a_repo_the_workbench_created_can_still_be_merged(self, repo,
                                                              capsys):
        """点『建仓库并开工』建出来的仓库没有身份 —— 合入不能因此永远走不通。

        2026-09-30 面板那条路实测：一句话 + 一个空目录 → 建仓库 → 切分 → 跑完 →
        判据全绿 → `accept` 因为取不到 user.name 拒绝。也就是说新建的项目**必然**
        卡在最后一步。判据用"基线提交就是这个署名"，不用"目录是我们建的"这种
        查不出来的事；换成人自己建的仓库（下一条那个 tester 署名）仍然拒。
        """
        git(repo, "config", "--local", "--unset", "user.name")
        git(repo, "config", "--local", "--unset", "user.email")
        fb = bp.GIT_FALLBACK_IDENTITY
        git(repo, "-c", f"user.name={fb[0]}", "-c", f"user.email={fb[1]}",
            "commit", "--amend", "--reset-author", "--no-edit", "-q")
        spec, state, patch = self._ready(repo, tmp_patch=True)
        before = bp.head(str(repo))
        capsys.readouterr()
        assert bp.accept(spec, state, confirmed=True) == 0
        assert bp.head(str(repo)) != before
        assert "沿用同一个身份" in capsys.readouterr().out
        ms = bp.milestone_state(bp.load_state(spec), "m1")
        assert ms["status"] == "done" and ms["committer"] == fb[0]
        # 不许把身份写进 config —— 那是替人做机器级的决定
        assert "user.name" not in (repo / ".git" / "config").read_text(
            encoding="utf-8")

    def test_accept_refuses_without_authorization(self, repo, capsys):
        spec, state, patch = self._ready(repo, tmp_patch=True)
        assert bp.accept(spec, state, confirmed=False, ask=lambda p: "n") == 2
        assert bp.head(str(repo)) == state["milestones"]["m1"]["base_before"]
        assert "未合入" in capsys.readouterr().out

    def test_accept_survives_no_tty_and_still_refuses(self, repo, capsys):
        spec, state, _ = self._ready(repo, tmp_patch=True)
        assert bp.accept(spec, state, confirmed=False, ask=_raise_eof) == 2
        assert "拿不到授权" in capsys.readouterr().out

    def test_accept_refuses_a_patch_that_changed_after_it_was_recorded(
            self, repo, capsys):
        spec, state, patch = self._ready(repo, tmp_patch=True)
        patch.write_text(patch.read_text(encoding="utf-8") + "\n"
                         "def extra():\n    return 1\n", encoding="utf-8")
        assert bp.accept(spec, state, confirmed=True) == 2
        assert "被改过" in capsys.readouterr().out
        assert state["milestones"]["m1"]["status"] == "awaiting-merge"

    def test_accept_with_yes_lands_a_commit_and_marks_done(self, repo, capsys):
        spec, state, patch = self._ready(repo, tmp_patch=True)
        before = bp.head(str(repo))
        assert bp.accept(spec, state, confirmed=True) == 0
        ms = state["milestones"]["m1"]
        assert bp.head(str(repo)) != before and ms["status"] == "done"
        assert (repo / "calc.py").read_text(encoding="utf-8").count("a * b") == 1
        out = capsys.readouterr().out
        assert "已合入并提交" in out and "patch sha256=" in out
        # 提交消息里要查得回"同意的是哪一版"
        subject = git(repo, "log", "-1", "--pretty=%s").stdout
        assert "milestone: m1" in subject and ms["patch_sha256"][:12] in subject

    def test_accept_stages_only_what_the_patch_touched(self, repo):
        spec, state, patch = self._ready(repo, tmp_patch=True)
        (repo / "无关文件.txt").write_text("别把它提交进去\n", encoding="utf-8")
        assert bp.accept(spec, state, confirmed=True) == 0
        files = git(repo, "show", "--pretty=", "--name-only").stdout.split()
        assert "无关文件.txt" not in files, "accept 用了 git add -A"

    def _ready(self, repo, tmp_patch=False):
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        body = ("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
                " def multiply(a, b):\n"
                "-    return a + b\n+    return a * b\n")
        patch = repo / "runtime_batch" / "changes.patch"
        patch.parent.mkdir(exist_ok=True)
        patch.write_text(body, encoding="utf-8")
        ms = bp.milestone_state(state, "m1")
        import hashlib
        ms.update(status="awaiting-merge", runtime_task_id="rt-x",
                  base_before=bp.head(str(repo)), patch=str(patch),
                  patch_sha256=hashlib.sha256(
                      patch.read_bytes()).hexdigest())
        bp.save_state(spec, state)
        return spec, state, patch


def _raise_eof(prompt):
    raise EOFError


class TestStateFile:
    def test_state_lives_under_runtime_batch_and_survives_corruption(self, repo):
        spec = bp.load_spec(write_spec(repo))
        p = bp.save_state(spec, {"name": "t", "workspace": str(repo),
                                 "milestones": {"m1": {"status": "done"}},
                                 "created_at": "", "final": {}})
        assert p.parent == repo / "runtime_batch"
        p.write_text("{ 坏掉的 json", encoding="utf-8")
        state = bp.load_state(spec)
        assert state["milestones"] == {}, "坏了还沿用半截状态会更糟"

    def test_status_renders_progress_and_next_step(self, repo):
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        text = bp.render_status(spec, state)
        assert "m1-multiply" not in text          # 例子里的 id 不出现在这
        assert "pending" in text and "批次判定" in text
        assert "下一步" in text


class TestFailedMilestoneIsNotSkipped:
    """跳过失败格继续跑 = 后面的里程碑长在一个不存在的结果上。"""

    def _two(self, repo):
        return bp.load_spec(write_spec(repo, milestones=[
            {"id": "m1", "goal": "把 multiply 改成返回 a * b", "acceptance": "x"},
            {"id": "m2", "goal": "给 divide 补一个除零测试", "acceptance": "y"}]))

    def test_next_step_refuses_to_jump_ahead(self, repo):
        spec = self._two(repo)
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "failed"
        target, why = bp.next_step(spec, state)
        assert target is None and "不会跳过" in why
        assert bp.submit_next(spec, state, serve=False) == 2

    def test_retry_puts_the_milestone_back_and_keeps_the_record(self, repo):
        spec = self._two(repo)
        state = bp.load_state(spec)
        ms = bp.milestone_state(state, "m1")
        ms.update(status="failed", runtime_task_id="rt-dead", detail="网络断了",
                  finished_at="2026-09-28T00:00:00")
        assert bp.retry(spec, state, "m1") == 0
        assert ms["status"] == "pending" and ms["runtime_task_id"] == ""
        assert ms["history"][0]["runtime_task_id"] == "rt-dead"
        assert ms["history"][0]["detail"] == "网络断了"
        target, _ = bp.next_step(spec, state)
        assert target["id"] == "m1"

    def test_retry_only_accepts_a_failed_milestone(self, repo, capsys):
        spec = self._two(repo)
        state = bp.load_state(spec)
        assert bp.retry(spec, state, "m1") == 2          # 还是 pending
        assert bp.retry(spec, state, "不存在") == 2
        assert "没有这个里程碑" in capsys.readouterr().out


class TestDeadSchedulerIsNotWaitedOut:
    """代起的调度器自己走了，父进程不该干等到 --timeout。

    真实形状：spec 指向一份 `scheduler.enabled` 不为 true 的配置时，子进程
    打印一句就退出，`run` 原本会一直轮到超时（默认 3600s）—— 而人正等着
    "人工确认后继续执行"这一步，白等一小时是最坏的结果。
    """

    class _Dead:
        log_path = Path("runtime_batch/dead.log")

        def running(self):
            return False

        def tail(self, n=40):
            return ["[scheduler] config 里 scheduler.enabled=false —— 调度层未启用"]

    class _AliveButIdle:
        """跳板 python.exe 还活着，真调度器早就退了 —— running 说 True。"""
        log_path = Path("runtime_batch/dead.log")

        def running(self):
            return True

        def tail(self, n=40):
            return ["[scheduler] config 里 scheduler.enabled=false —— 调度层未启用"]

    def _run(self, monkeypatch, repo, runner, grace=0.05):
        from tools import delivery_view as dv

        monkeypatch.setattr(dv, "queue_rows", lambda cd, limit=200: [
            {"runtime_task_id": "rt-q", "status": "QUEUED", "last_error": ""}])
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        target = spec["milestones"][0]
        ms = bp.milestone_state(state, target["id"])
        ms.update(status="queued", runtime_task_id="rt-q")
        return bp.watch_one(spec, state, target, ms, 0.01, 30.0, print,
                            runner=runner, queue_grace=grace)

    def test_it_stops_and_shows_why(self, repo, monkeypatch, capsys):
        rc = self._run(monkeypatch, repo, self._Dead())
        out = capsys.readouterr().out
        assert rc == 1
        assert "调度器已经退出" in out or "没有调度器在领" in out
        assert "scheduler.enabled=false" in out

    def test_a_live_looker_but_idle_runner_still_stops_being_watched(
            self, repo, monkeypatch, capsys):
        """process liveness 是错的信号：这台机器的 python.exe 是跳板。"""
        rc = self._run(monkeypatch, repo, self._AliveButIdle())
        out = capsys.readouterr().out
        assert rc == 1
        assert "没有调度器在领" in out and "scheduler.enabled" in out

    def test_a_running_slice_is_never_called_stuck(self, repo, monkeypatch):
        """真跑起来的一格可能跑很久 —— 不许用"没动"打断它。"""
        from tools import delivery_view as dv

        monkeypatch.setattr(dv, "queue_rows", lambda cd, limit=200: [
            {"runtime_task_id": "rt-q", "status": "RUNNING", "last_error": ""}])
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        target = spec["milestones"][0]
        ms = bp.milestone_state(state, target["id"])
        ms.update(status="queued", runtime_task_id="rt-q")
        rc = bp.watch_one(spec, state, target, ms, 0.01, 0.08,
                          lambda *a: None, runner=self._Dead(),
                          queue_grace=0.01)
        assert rc == 1                       # 超时退出
        assert ms["status"] == "queued"      # 但没被判成"没人领"

    def test_the_slice_is_left_failed_not_awaiting(self, repo, monkeypatch):
        rc = self._run(monkeypatch, repo, self._Dead())
        spec = bp.load_spec(write_spec(repo))
        ms = bp.milestone_state(bp.load_state(spec), "m1")
        assert rc == 1 and ms["status"] == "failed"
        assert "无人领取" in ms["detail"] or "调度器" in ms["detail"]

    def test_no_runner_means_no_dead_check(self, repo, monkeypatch):
        """--no-serve 时没有 runner —— 那时别人开着，不许误判成死了。"""
        from tools import delivery_view as dv

        calls = {"n": 0}
        rows = [{"runtime_task_id": "rt-q", "status": "QUEUED", "last_error": ""}]

        def spy(cd, limit=200):
            calls["n"] += 1
            return rows
        monkeypatch.setattr(dv, "queue_rows", spy)
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        target = spec["milestones"][0]
        ms = bp.milestone_state(state, target["id"])
        ms.update(status="queued", runtime_task_id="rt-q")
        rc = bp.watch_one(spec, state, target, ms, 0.01, 0.05, lambda *a: None)
        assert rc == 1 and calls["n"] > 1        # 轮到超时，而不是判死


class TestPromptIsRecorded:
    """验收 agent 交给执行 agent 的那段话必须留下原文。

    核心按设计不落盘 prompt，所以这段话只有在批次这一层自己记下来才谈得上"可见可控"。
    这里刻意只 stub 最外层的 service —— 真实的 submit_one 与真实的 goal_for 都参与，
    断言的是"它交出去的那一份"与"它存下来的那一份"是同一个字符串。
    （真实档另有一次零配额实跑证明这条线在真提交路径上成立：
    `runtime_batch/demo-calculator.json` 的 m1-multiply 带 prompt + prompt_sha256。）
    """

    def test_submit_stores_the_exact_text_handed_to_the_executor(self, repo,
                                                                monkeypatch):
        import tools.scheduler_cli as sc

        seen = {}

        class FakeRT:
            runtime_task_id = "rt-fake"
            workspace_strategy = "COPY"

        def fake_service(config, r):
            class S:
                def submit(self, task, **kw):
                    seen["goal"] = task.goal
                    return FakeRT()
            return S()

        monkeypatch.setattr(sc, "build_submission_service", fake_service)
        monkeypatch.setattr(sc, "build_repo_from_config",
                            lambda c: type("R", (), {"close": lambda s: None})())
        spec = bp.load_spec(write_spec(repo, config_dir="examples/config_minimal"))
        state = bp.load_state(spec)
        assert bp.submit_next(spec, state, wait=False,
                              echo=lambda *a, **k: None) == 0
        ms = bp.milestone_state(state, "m1")
        assert seen.get("goal"), "没有真的把提示词交出去"
        assert ms["prompt"] == seen["goal"], "存下来的不是交出去那一份"
        assert ms["prompt_sha256"] == hashlib.sha256(
            seen["goal"].encode("utf-8")).hexdigest()
        # 内容本身要带得上验收命令，否则"可见"只是把 goal 抄了一遍。
        # 清单位置那段由 TestGoalCarriesTheChecklist 负责，这里不重复断言
        # （单里程碑的 spec 本来也不会出现"第 1 个"那句）。
        assert "验收命令" in ms["prompt"]


class TestRunBringsItsOwnScheduler:
    """调度器在队列空时会自己退出，所以 run 必须自带一个，而且用完要关。"""

    class _RT:
        def __init__(self, sink):
            self.sink = sink
            self.log_path = Path("runtime_batch/x.log")

        def start(self):
            self.sink.append("start")
            return True, "已启动 pid=1"

        def stop(self):
            self.sink.append("stop")
            return False, "已停止"

    def _wire(self, monkeypatch, sink, watch_rc):
        import tools.scheduler_cli as sc

        class FakeRT:
            runtime_task_id = "rt-fake"
            workspace_strategy = "GIT_WORKTREE"

        def fake_service(config, repo):
            class S:
                def submit(self, task, **kw):
                    return FakeRT()
            return S()

        monkeypatch.setattr(sc, "build_submission_service", fake_service)
        monkeypatch.setattr(sc, "build_repo_from_config",
                            lambda c: type("R", (), {"close": lambda s: None})())
        monkeypatch.setattr(bp, "watch_one",
                            lambda *a, **k: (sink.append("watch"), watch_rc)[1])

    def test_started_before_waiting_and_stopped_after(self, repo, monkeypatch,
                                                      capsys):
        sink: list[str] = []
        self._wire(monkeypatch, sink, 0)
        spec = bp.load_spec(write_spec(repo, config_dir="examples/config_minimal"))
        state = bp.load_state(spec)
        rc = bp.submit_next(spec, state, runner_factory=lambda cd: self._RT(sink))
        assert rc == 0
        assert sink == ["start", "watch", "stop"], sink

    def test_stop_happens_even_on_timeout(self, repo, monkeypatch):
        sink: list[str] = []
        self._wire(monkeypatch, sink, 1)
        spec = bp.load_spec(write_spec(repo, config_dir="examples/config_minimal"))
        state = bp.load_state(spec)
        assert bp.submit_next(spec, state,
                              runner_factory=lambda cd: self._RT(sink)) == 1
        assert sink[-1] == "stop", "超时就把调度器留在外面跑着"

    def test_no_serve_does_not_spawn_anything(self, repo, monkeypatch):
        sink: list[str] = []
        self._wire(monkeypatch, sink, 0)
        spec = bp.load_spec(write_spec(repo, config_dir="examples/config_minimal"))
        state = bp.load_state(spec)
        bp.submit_next(spec, state, serve=False,
                       runner_factory=lambda cd: self._RT(sink))
        assert sink == ["watch"], sink


class TestResumeAfterTheWorkerDied:
    """**已经交出去过**的一格：干活的人没了，批次必须能接着跑，而不是永远拒。

    现场（2026-09-30，真实那一跑）：面板与推进器随一次工具调用的进程树被回收，
    m1 停在 `RUNNING`、租约心跳之后再没人续。`ship` 看见状态是 running 就按
    "一次只推进一格"拒绝，于是既没人跑也没人等 —— 业主看到的正是"跑到一半没了动静"。
    判据用**租约**而不是状态字：RUNNING + 过期租约 = 那个人已经死了。
    """

    class _Task:
        def __init__(self, value):
            self.status = type("S", (), {"value": value})()

    class _Repo:
        def __init__(self, value, expired):
            self.value, self.expired = value, expired
            self.closed = False

        def get(self, rt):
            return None if self.value == "NONE" else (
                TestResumeAfterTheWorkerDied._Task(self.value))

        def lease_expired(self, rt):
            return self.expired

        def close(self):
            self.closed = True

    def _wire(self, monkeypatch, sink, repo_value, expired, watch_rc=0):
        import tools.scheduler_cli as sc

        def fake_build_repo(config):
            sink.append(("repo", repo_value, expired))
            return self._Repo(repo_value, expired)

        def boom(config, r):
            raise AssertionError("接管那一支不该重新提交 —— 那会再花一次额度")

        monkeypatch.setattr(sc, "build_repo_from_config", fake_build_repo)
        monkeypatch.setattr(sc, "build_submission_service", boom)
        monkeypatch.setattr(bp, "watch_one",
                            lambda *a, **k: (sink.append("watch"), watch_rc)[1])

    def _queued_state(self, repo):
        spec = bp.load_spec(write_spec(repo, config_dir="examples/config_minimal"))
        state = bp.load_state(spec)
        bp.milestone_state(state, spec["milestones"][0]["id"]).update(
            status="queued", runtime_task_id="rt-dead")
        return spec, state

    def test_an_expired_lease_is_taken_over_by_a_scheduler(self, repo, monkeypatch):
        sink: list[str] = []
        self._wire(monkeypatch, sink, "RUNNING", expired=True)
        spec, state = self._queued_state(repo)
        rc = bp.submit_next(spec, state,
                            runner_factory=lambda cd: self._RT(sink))
        assert rc == 0, "干活的人已经死了却仍然拒绝推进 —— 批次就此钉死"
        assert sink[1:4] == ["start", "watch", "stop"], sink

    def test_a_queued_task_with_no_taker_is_also_helped(self, repo, monkeypatch):
        sink: list[str] = []
        self._wire(monkeypatch, sink, "QUEUED", expired=True)
        spec, state = self._queued_state(repo)
        assert bp.submit_next(spec, state,
                              runner_factory=lambda cd: self._RT(sink)) == 0
        assert "start" in sink

    def test_a_live_lease_is_still_refused(self, repo, monkeypatch):
        """有人在跑就不许抢 —— 判据反过来也得成立，否则这条改动只是"永远放行"。"""
        sink: list[str] = []
        self._wire(monkeypatch, sink, "RUNNING", expired=False)
        spec, state = self._queued_state(repo)
        rc = bp.submit_next(spec, state,
                            runner_factory=lambda cd: self._RT(sink))
        assert rc == 2
        assert sink == [("repo", "RUNNING", False)], sink
        assert "start" not in sink

    def test_awaiting_merge_never_starts_a_scheduler(self, repo, monkeypatch):
        """那一格在等人合入（或等 auto 档的 accept），不是等人干活。"""
        sink: list[str] = []
        self._wire(monkeypatch, sink, "COMPLETED", expired=True)
        spec = bp.load_spec(write_spec(repo, config_dir="examples/config_minimal"))
        state = bp.load_state(spec)
        bp.milestone_state(state, spec["milestones"][0]["id"]).update(
            status="awaiting-merge", runtime_task_id="rt-done")
        assert bp.submit_next(spec, state,
                              runner_factory=lambda cd: self._RT(sink)) == 2
        assert "start" not in sink

    def test_worker_state_reports_missing_instead_of_guessing(self, monkeypatch):
        import tools.scheduler_cli as sc

        def boom(config):
            raise RuntimeError("没有这个配置")
        monkeypatch.setattr(sc, "build_repo_from_config", boom)
        assert bp.worker_state("config", "rt-x") == "missing"
        assert bp.worker_state("config", "") == "missing"

    class _RT:
        def __init__(self, sink):
            self.sink = sink

        def running(self):
            return "start" in self.sink

        def log_path(self):
            return "runtime_batch/x/scheduler.log"

        def start(self):
            self.sink.append("start")
            return True, "已启动"

        def stop(self):
            self.sink.append("stop")
            return False, "已停止"


class TestDemo:
    """demo 是"同意之前看一眼"，所以它跑完就得停 —— 不留常驻进程。"""

    def test_not_declared_is_said_not_invented(self, repo):
        assert bp.run_demo({"id": "m1"}, str(repo))["status"] == "not-declared"

    def test_demo_runs_in_the_workspace_and_lists_what_it_produced(self, repo):
        target = {"id": "m1", "demo": {"command": [
            "python", "-c", "open('demo.html','w').write('<h1>ok</h1>')"]}}
        out = bp.run_demo(target, str(repo), echo=lambda *a, **k: None)
        assert out["status"] == "ok" and out["exit_code"] == 0
        assert "demo.html" in out["produced"]

    def test_a_failing_demo_is_recorded_as_failing(self, repo):
        target = {"id": "m1", "demo": {"command": [
            "python", "-c", "import sys;sys.exit(4)"]}}
        out = bp.run_demo(target, str(repo), echo=lambda *a, **k: None)
        assert out["status"] == "fail" and out["exit_code"] == 4

    def test_a_demo_that_does_not_stop_is_reported_as_such(self, repo):
        target = {"id": "m1", "demo": {"command": [
            "python", "-c", "import time;time.sleep(30)"]}}
        out = bp.run_demo(target, str(repo), echo=lambda *a, **k: None,
                          timeout=1.0)
        assert out["status"] == "timeout"

    def test_bad_demo_shape_is_refused_at_load_time(self, tmp_path):
        p = write_spec(tmp_path, milestones=[{
            "id": "m1", "goal": "把 multiply 改成返回 a * b",
            "acceptance": "pytest -q", "demo": ["python", "serve.py"]}])
        with pytest.raises(bp.BatchError, match="demo"):
            bp.load_spec(p)


class TestDeclaredIndependence:
    """`depends_on` 让人不被无关的待合格卡住 —— 但只在显式声明时。"""

    def _spec(self, repo, m2_dep="__unset__"):
        m2 = {"id": "m2", "goal": "把特性数据接进页面并生成子页",
              "acceptance": "pytest tests/test_render.py -q"}
        if m2_dep != "__unset__":
            m2["depends_on"] = m2_dep
        return bp.load_spec(write_spec(repo, milestones=[
            {"id": "m1", "goal": "建首页骨架 index.html 与样式",
             "acceptance": "pytest tests/test_structure.py -q"},
            m2,
            {"id": "m3", "goal": "新增关于页 about.html 并补全首页导航链接",
             "acceptance": "pytest tests/test_nav.py -q",
             "depends_on": ["m1", "m2"]}]))

    def test_default_still_blocks_on_an_unmerged_predecessor(self, repo):
        spec = self._spec(repo)
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "awaiting-merge"
        bp.save_state(spec, state)
        nxt, why = bp.next_step(spec, bp.load_state(spec))
        assert nxt["id"] == "m1" and "先 accept" in why

    def test_a_declared_independent_slice_may_run_while_m1_waits(self, repo):
        spec = self._spec(repo, m2_dep=[])
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "awaiting-merge"
        bp.save_state(spec, state)
        nxt, why = bp.next_step(spec, bp.load_state(spec))
        assert nxt["id"] == "m2"
        assert "不依赖 m1" in why and "合入仍然要人点头" in why

    def test_a_slice_that_needs_an_unmerged_predecessor_still_waits(self, repo):
        spec = self._spec(repo)
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "awaiting-merge"
        bp.milestone_state(state, "m2")["status"] = "awaiting-merge"
        bp.save_state(spec, state)
        nxt, why = bp.next_step(spec, bp.load_state(spec))
        assert nxt["id"] == "m1" and nxt["id"] != "m3"

    def test_a_failed_slice_is_never_jumped_over(self, repo):
        """声明独立也不能跨过失灵的结果 —— 那是死路，不是排队。"""
        spec = self._spec(repo, m2_dep=[])
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "failed"
        bp.save_state(spec, state)
        nxt, why = bp.next_step(spec, bp.load_state(spec))
        assert nxt is None and "run --retry m1" in why

    @pytest.mark.parametrize("dep,needle", [
        (["m3"], "循环依赖"),
        (["m2"], "循环依赖"),
        (["nope"], "不存在的 id"),
        ("m1", "数组"),
    ])
    def test_bad_dependency_declarations_are_refused(self, repo, tmp_path, dep,
                                                     needle):
        import json as _json
        m = {"id": "m1", "goal": "建首页骨架 index.html 与样式",
             "acceptance": "pytest -q"}
        m2 = {"id": "m2", "goal": "把特性数据接进页面并生成子页",
              "acceptance": "pytest -q", "depends_on": dep}
        m3 = {"id": "m3", "goal": "新增关于页 about.html 并补全导航链接",
              "acceptance": "pytest -q"}
        p = tmp_path / "project.json"
        p.write_text(_json.dumps({"name": "t", "workspace": str(repo),
                                  "milestones": [m, m2, m3]}, ensure_ascii=False),
                     encoding="utf-8")
        with pytest.raises(bp.BatchError, match=needle):
            bp.load_spec(p)


class TestFailedSliceIsNotADeadEnd:
    """判红但有货的那一格：以前 recheck 不接、advance 也不接，批次永久卡死。"""

    def _failed(self, repo, patch=None, rt="rt-f1"):
        spec = bp.load_spec(write_spec(repo))
        state = bp.load_state(spec)
        ms = bp.milestone_state(state, "m1")
        ms.update(status="failed", runtime_task_id=rt, base_before=bp.head(repo),
                  detail="executor envelope invalid")
        if patch:
            ms["patch"] = str(patch)
        bp.save_state(spec, state)
        return spec, state

    def test_advance_refuses_a_failed_slice_without_a_patch(self, repo, capsys):
        spec, state = self._failed(repo)
        (repo / "calc.py").write_text("def multiply(a, b):\n    return a * b\n",
                                      encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-q",
            "-m", "hand merge")
        assert bp.advance(spec, bp.load_state(spec)) == 2
        assert "recheck" in capsys.readouterr().out

    def test_advance_refuses_when_the_patched_files_are_not_there(self, repo,
                                                                  capsys):
        patch = repo / "runtime_batch" / "p.patch"
        patch.parent.mkdir(exist_ok=True)
        patch.write_text("--- a/ghost.py\n+++ b/ghost.py\n@@ -0,0 +1 @@\n+x\n",
                         encoding="utf-8")
        spec, state = self._failed(repo, patch=patch)
        git(repo, "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-q",
            "--allow-empty", "-m", "unrelated")
        assert bp.advance(spec, bp.load_state(spec)) == 2
        assert "ghost.py" in capsys.readouterr().out

    def test_a_hand_merged_failed_slice_can_be_recorded_and_says_it_was_unreviewed(
            self, repo, capsys):
        spec, state = self._failed(repo, patch=None)   # base_before = 现在
        (repo / "calc.py").write_text("def multiply(a, b):\n    return a * b\n",
                                      encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-q",
            "-m", "hand merge")
        patch = repo / "runtime_batch" / "p.patch"
        patch.parent.mkdir(exist_ok=True)
        patch.write_text("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
                         " def multiply(a, b):\n"
                         "-    return a + b\n+    return a * b\n", encoding="utf-8")
        state["milestones"]["m1"]["patch"] = str(patch)
        bp.save_state(spec, state)
        assert bp.advance(spec, bp.load_state(spec)) == 0
        out = capsys.readouterr().out
        assert "Reviewer 没有通过" in out and "human-unreviewed" in out
        ms = bp.milestone_state(bp.load_state(spec), "m1")
        assert ms["status"] == "done" and ms["merged_by"] == "human-unreviewed"

    def test_an_awaiting_merge_slice_still_wins_over_a_failed_one(self, repo):
        spec = bp.load_spec(write_spec(repo, milestones=[
            {"id": "m1", "goal": "把 multiply 改成返回 a * b", "acceptance": "x"},
            {"id": "m2", "goal": "给 divide 补一个除零测试", "acceptance": "y"}]))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1").update(status="failed",
                                               runtime_task_id="rt-f")
        bp.milestone_state(state, "m2").update(status="awaiting-merge",
                                               runtime_task_id="rt-a")
        bp.save_state(spec, state)
        bp.advance(spec, bp.load_state(spec))
        st = bp.load_state(spec)["milestones"]
        assert st["m2"].get("status") in ("awaiting-merge", "done")
        assert st["m1"]["status"] == "failed"

    def test_recheck_falls_back_to_the_failed_slice_without_promoting_it(
            self, repo, monkeypatch, capsys):
        from tools import delivery_view as dv

        exec_ws, real_patch = TestRecheck()._worktree_with_only_new_files(
            repo, rt="rt-f1")
        assert real_patch.is_file()
        spec, state = self._failed(repo, rt="rt-f1")
        monkeypatch.setattr(dv, "collect",
                            lambda rt, cd: ({"patch": str(real_patch)}, ""))
        rc = bp.recheck(spec, bp.load_state(spec))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Reviewer 没有通过" in out and "accept 比对" not in out
        ms = bp.milestone_state(bp.load_state(spec), "m1")
        assert ms["status"] == "failed", "不许把没评审的一格抬成待合入"
        assert ms["patch"] != str(real_patch)     # 新补丁落在 recheck/ 下
        assert "run --retry m1" in out


class TestRecheck:
    """重取证据：收集器修好之后，已跑完的那一格还得有一条把补丁重建出来的路。

    现场（2026-09-28）：m1 建出 index.html，Reviewer 判 pass，`changes.patch` 却是
    0 行 —— 未跟踪文件不进 `git diff`。这一格不该因为采集时机已过就永远合不了。
    """

    def _worktree_with_only_new_files(self, repo, rt="rt-r"):
        from mao.workspaces import WorkspaceStrategy, WorkspaceStrategyManager
        mgr = WorkspaceStrategyManager(worktree_root=repo / "wt")
        base = mgr.validate_for_submission(repo,
                                           WorkspaceStrategy.GIT_WORKTREE)
        plan = mgr.prepare(runtime_task_id=rt, source_path=repo,
                           strategy=WorkspaceStrategy.GIT_WORKTREE,
                           base_revision=base["base_revision"], now_iso="now")
        exec_ws = Path(plan.execution_workspace_path)
        (exec_ws / "index.html").write_text("<html>new</html>\n",
                                            encoding="utf-8")
        arts = repo / "arts" / rt
        result = mgr.collect_result(plan, arts)
        return exec_ws, Path(result["changes_patch"])

    def test_recheck_rebuilds_the_empty_patch_and_accept_lands_it(self, repo,
                                                                 capsys):
        spec = bp.load_spec(write_spec(
            repo, strategy="GIT_WORKTREE", milestones=[
                {"id": "m1", "goal": "做一个可打开的静态首页 index.html",
                 "acceptance": "pytest -q"}]))
        state = bp.load_state(spec)
        exec_ws, real_patch = self._worktree_with_only_new_files(repo)
        assert "+<html>new</html>" in real_patch.read_text(encoding="utf-8")

        # 模拟修复前的取证结果：文件在，补丁 0 行，sha 记的是空串
        broken = real_patch.parent / "changes.patch"
        broken.write_text("", encoding="utf-8")
        ms = bp.milestone_state(state, "m1")
        ms.update(status="awaiting-merge", runtime_task_id="rt-r",
                  patch=str(broken), patch_sha256=bp._sha(broken),
                  execution_workspace=str(exec_ws),
                  base_before=bp.head(repo))
        bp.save_state(spec, state)

        assert bp.accept(spec, bp.load_state(spec), confirmed=True,
                         echo=lambda *a, **k: None) == 1, "空补丁本就不该合"

        assert bp.recheck(spec, bp.load_state(spec)) == 0
        out = capsys.readouterr().out
        assert "0 行" in out and "改动文件：index.html" in out
        after = bp.load_state(spec)["milestones"]["m1"]
        new_patch = Path(after["patch"])
        assert new_patch != broken and new_patch.is_file()
        text = new_patch.read_text(encoding="utf-8")
        assert text.count("new file mode") == 1 and "+<html>new</html>" in text
        assert after["patch_sha256"] == bp._sha(new_patch)
        # 原证据不许被改写 —— 重取只能新增
        assert broken.read_text(encoding="utf-8") == ""

        assert bp.accept(spec, bp.load_state(spec), confirmed=True) == 0
        assert "<html>new</html>" in (repo / "index.html").read_text(
            encoding="utf-8")

    def test_recheck_refuses_when_the_worktree_is_gone(self, repo, capsys):
        spec = bp.load_spec(write_spec(repo, strategy="GIT_WORKTREE"))
        state = bp.load_state(spec)
        arts = repo / "arts" / "rt-gone"
        arts.mkdir(parents=True)
        arts.joinpath("changes.patch").write_text("", encoding="utf-8")
        (arts / "workspace_result.json").write_text(json.dumps(
            {"workspace_strategy": "GIT_WORKTREE",
             "execution_workspace_path": str(repo / "nope"),
             "base_revision": ""}), encoding="utf-8")
        ms = bp.milestone_state(state, "m1")
        ms.update(status="awaiting-merge", runtime_task_id="rt-gone",
                  patch=str(arts / "changes.patch"),
                  patch_sha256=bp._sha(arts / "changes.patch"))
        bp.save_state(spec, state)

        assert bp.recheck(spec, bp.load_state(spec)) == 2
        assert "已经不在了" in capsys.readouterr().out
        # 拒绝时记录不动：sha 还是那份，accept 仍然会拦
        assert bp.load_state(spec)["milestones"]["m1"]["patch_sha256"] == \
            bp._sha(arts / "changes.patch")

    def test_recheck_says_so_when_there_is_nothing_to_repair(self, repo,
                                                            capsys):
        spec = bp.load_spec(write_spec(repo))
        assert bp.recheck(spec, bp.load_state(spec)) == 2
        assert "没有等待合入" in capsys.readouterr().out


# ===========================================================================
# plan —— 一句目标交给 Supervisor 拆成里程碑清单
# ===========================================================================
def plan_answer(**over):
    """一份"形状正确"的 Supervisor 回答：键与 load_spec 一一对齐。

    workspace 故意填一个不存在的路径 —— plan 必须用 --workspace 覆盖它，
    落点不能跟着模型的手走。
    """
    answer = {
        "name": "plan-t",
        "workspace": "这个路径不该被采信",
        "strategy": "COPY",
        "config_dir": "examples/config_minimal",
        "max_rounds": 2,
        "constraints": ["不得修改验收测试文件"],
        "final_acceptance": {"name": "full-suite", "command": ["pytest", "-q"]},
        "milestones": [
            {"id": "m1",
             "goal": "把 calc.py 的 multiply 改成返回 a * b，其余函数不动",
             "acceptance": "pytest tests/test_m1_multiply.py -q"},
            {"id": "m2",
             "goal": "给 calc.py 的 divide 补一个除数为 0 抛错的验收测试",
             "acceptance": "pytest tests -q",
             "demo": {"command": ["python", "-c", "print('ok')"]}},
        ],
    }
    answer.update(over)
    return answer


@pytest.fixture()
def supervisor_replies(monkeypatch):
    """替换**唯一**那个"调用 Supervisor 角色"的函数 —— 真实 CLI 由此不可达。"""
    seen: list[str] = []

    def _install(payload):
        text = (payload if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=False))

        def fake(prompt, **_kw):
            seen.append(prompt)
            return text

        monkeypatch.setattr(bp, "ask_supervisor", fake)
    _install.seen = seen
    return _install


def _refused(capsys) -> str:
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("没有生成项目档")]
    assert len(lines) == 1, f"拒绝时只该给一句可照着做的话：{out}"
    return lines[0]


class TestPlan:
    def test_well_formed_answer_lands_a_project_run_can_pick_up(
            self, repo, supervisor_replies, capsys):
        supervisor_replies(plan_answer())
        target = repo / "plans" / "project.json"
        rc = bp.plan(target, "把计算器修好并补上除零的验收测试",
                     workspace=str(repo), mock=True)
        assert rc == 0
        # 请求文本确实是渲染过的模板（目标在里面，落点也在里面）
        assert "把计算器修好并补上除零的验收测试" in supervisor_replies.seen[0]

        spec = bp.load_spec(target)                       # 同一个判据再过一次
        assert spec["workspace"] == str(repo), "落点被回答里的路径带走了"
        assert spec["config_dir"] == "examples/config_minimal"
        state = bp.load_state(spec)
        nxt, _ = bp.next_step(spec, state)
        assert nxt["id"] == "m1", "run 该从拆出来的第一条开始"
        assert "2 条里程碑" in capsys.readouterr().out

    @pytest.mark.parametrize("mutate,needle", [
        (lambda m: m[0].__setitem__("acceptance", "首页看起来漂亮就行"), "描述"),
        (lambda m: m[0].pop("acceptance"), "acceptance"),
        (lambda m: m[1].__setitem__("id", "m1"), "重复"),
        (lambda m: m[0].__setitem__("demo", ["python", "serve.py"]), "demo"),
        (lambda m: m[0].__setitem__("owner", "执行者自己写验收"), "无法识别的键"),
    ])
    def test_a_bad_checklist_is_refused_and_nothing_is_written(
            self, repo, supervisor_replies, capsys, mutate, needle):
        answer = plan_answer()
        mutate(answer["milestones"])
        supervisor_replies(answer)
        target = repo / "project.json"
        assert bp.plan(target, "把计算器修好并补上测试", workspace=str(repo),
                       mock=True) == 2
        assert not target.exists(), "拒绝之后还留下了半个项目档"
        assert needle in _refused(capsys)

    def test_non_json_garbage_is_one_sentence_and_writes_nothing(
            self, repo, supervisor_replies, capsys):
        supervisor_replies("这个需求信息不足，请先补充以下几点：……")
        target = repo / "project.json"
        assert bp.plan(target, "做个首页", workspace=str(repo), mock=True) == 2
        assert not target.exists()
        assert "JSON" in _refused(capsys)

    def test_an_existing_project_file_needs_force(self, repo,
                                                  supervisor_replies, capsys):
        target = write_spec(repo, name="hand-written")
        before = target.read_text(encoding="utf-8")
        supervisor_replies(plan_answer())
        assert bp.plan(target, "重拆一次", workspace=str(repo),
                       mock=True) == 2
        assert target.read_text(encoding="utf-8") == before
        assert "不覆盖" in capsys.readouterr().out

        # --force 时才重写，且 workspace 从旧项目档继承（不用再敲一遍）
        assert bp.plan(target, "重拆一次", mock=True, force=True) == 0
        spec = bp.load_spec(target)
        assert spec["name"] == "plan-t" and spec["workspace"] == str(repo)

    def test_no_workspace_means_no_call_and_no_file(self, repo,
                                                    supervisor_replies, capsys):
        supervisor_replies(plan_answer())
        target = repo / "fresh.json"
        assert bp.plan(target, "把计算器修好", mock=True) == 2
        assert not target.exists()
        assert supervisor_replies.seen == []        # 连一次调用都没发起
        assert "workspace" in capsys.readouterr().out

    def test_the_rendered_prompt_carries_the_json_contract_and_the_command_rule(
            self):
        """模板与 load_spec 的耦合必须在这里被看见 —— 否则漂移只会在真实调用里炸。"""
        text = bp.project_plan_prompt("做一个能打开的静态首页",
                                      "examples/calculator",
                                      "examples/config_minimal")
        for key in bp.SPEC_KEYS | bp.MILESTONE_KEYS:
            assert key in text, f"模板里没有 {key} —— 判据与契约已经分叉"
        assert '"acceptance": "pytest tests/test_m1.py -q"' in text
        assert "machine-executable command" in text
        assert "Prose is rejected" in text
        assert "做一个能打开的静态首页" in text
        assert "examples/calculator" in text
        assert "{goal}" not in text and "{workspace_summary}" not in text

    def test_the_mock_provider_answers_through_the_real_seam(
            self, repo, capsys):
        """不替换任何函数：Mock provider 零配额走完整条链，且不落盘。

        内置 Mock 的剧本答的是 Plan（不是项目档），所以同一个 load_spec 会
        把它拒掉 —— 这条测试锁两件事：mock 绑定真的生效（没有子进程），
        以及"链路通"不等于"产物能用"。
        """
        offline = str(bp.ROOT / "config_offline")
        text = bp.ask_supervisor("plan a project", goal="g", workspace=str(repo),
                                 config_dir=offline, mock=True)
        assert json.loads(text)["executor_prompt"], "Mock Supervisor 没应答"

        target = repo / "project.json"
        assert bp.plan(target, "把计算器修好并补上测试", workspace=str(repo),
                       config_dir=offline, mock=True) == 2
        assert not target.exists()
        assert "无法识别的键" in _refused(capsys)


class TestProjectPlanTemplate:
    """模板文件与校验器的反漂移链接：键名必须同时存在于两边。"""

    def test_template_lists_every_key_the_validator_accepts(self):
        text = bp.PROJECT_PLAN_FILE.read_text(encoding="utf-8")
        for key in bp.SPEC_KEYS | bp.MILESTONE_KEYS:
            assert key in text, f"{key} 不在模板里 —— Supervisor 会被教去产出一份判据不要的档"

    def test_template_is_utf8_and_lf_only(self):
        raw = bp.PROJECT_PLAN_FILE.read_bytes()
        assert b"\r" not in raw, "索引是 LF；工作树 CRLF 会让按字节的守卫转红"

    def test_template_forbids_dropping_uncheckable_requirements(self):
        """真实档实测：目标里那句"中文"在拆解时丢了，页面全英文还全绿。"""
        text = bp.PROJECT_PLAN_FILE.read_text(encoding="utf-8")
        assert "Do not drop a stated requirement" in text


class TestGoalCarriesTheChecklist:
    """执行者要读的是**整张清单**，不是它那一行。"""

    def _spec(self):
        return {"name": "p", "workspace": ".", "milestones": [
            {"id": "m1", "goal": "建首页骨架 index.html 与样式",
             "acceptance": "pytest tests/test_a.py -q"},
            {"id": "m2", "goal": "把特性数据接进页面并生成子页",
             "acceptance": "pytest tests/test_b.py -q"},
            {"id": "m3", "goal": "补关于页与导航链接",
             "acceptance": "pytest tests/test_c.py -q"}]}

    def test_the_owner_words_reach_every_milestone(self):
        """拆解会丢限定词，所以原话每次都直接给执行者 —— 实测过的洞。

        真实档：目标写"中文演示站点"，切出来的 milestone goal 里没有"中文"，
        于是规划角色也没生成任何语言相关的验收标准，页面全英文还全绿。
        """
        spec = self._spec()
        spec["owner_goal"] = "交付一个可打开的中文演示站点"
        text = bp.goal_for(spec, spec["milestones"][1])
        assert text.startswith("项目总目标（业主原话）：交付一个可打开的中文演示站点")
        assert "本格要做的是：把特性数据接进页面" in text

    def test_no_owner_goal_keeps_the_text_as_before(self):
        spec = self._spec()
        assert "业主原话" not in bp.goal_for(spec, spec["milestones"][0])

    def test_save_state_carries_the_owner_words_into_the_state_file(self, repo):
        """面板读的是状态文件。原话只留在 spec 里，核查那一格就永远印不出原话。

        这条是被真实档打出来的：UI 测试自己手写了带 owner_goal 的状态文件，
        于是它一直绿，而真实 `save_state` 从来没这个键。
        """
        spec = bp.load_spec(write_spec(repo, owner_goal="交付一个中文演示站点"))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "awaiting-merge"
        p = bp.save_state(spec, state)
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        assert on_disk["owner_goal"] == "交付一个中文演示站点"

    def test_save_state_carries_the_verdict_the_cli_computes(self, repo):
        """判定此前只活在 CLI 的 status 输出里，面板读状态文件 —— 于是界面上
        从来没有"项目完成"这三个字。同一形状的第二例（见 owner_goal）。
        """
        spec = bp.load_spec(write_spec(
            repo, owner_goal="交付一个中文演示站点",
            final_acceptance={"name": "x",
                              "command": ["python", "-c", "import sys;sys.exit(0)"]}))
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1")["status"] = "done"
        state["final"] = {"status": "pass", "exit_code": 0}
        p = bp.save_state(spec, state)
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        assert on_disk["verdict"] == bp.verdict(spec, state) == "项目完成"

    def test_without_state_it_stays_the_old_single_line(self):
        spec = self._spec()
        text = bp.goal_for(spec, spec["milestones"][0])
        assert "第" not in text and "后面还有" not in text

    def test_middle_milestone_sees_what_came_before_and_after(self):
        spec = self._spec()
        state = {"milestones": {"m1": {"status": "done"},
                                "m2": {"status": "pending"},
                                "m3": {"status": "pending"}}}
        text = bp.goal_for(spec, spec["milestones"][1], state)
        assert "3 个里程碑里的第 2 个（m2）" in text
        assert "前面已交付：m1" in text and "接着用，别重做" in text
        assert "后面还有：m3" in text and "别替它们做" in text

    def test_an_unmerged_previous_slice_is_not_called_delivered(self):
        """awaiting-merge 还没进仓库 —— 说"已交付"就是在骗执行者。"""
        spec = self._spec()
        state = {"milestones": {"m1": {"status": "awaiting-merge"},
                                "m2": {"status": "pending"}}}
        text = bp.goal_for(spec, spec["milestones"][1], state)
        assert "前面已交付" not in text
        assert "后面还有：m1、m3" in text


class TestPlanNameIsFoldedIntoASlug:
    """name 会变成 runtime_batch/ 下的状态文件名 —— 但**不许因此拒掉整次切分**。

    原来这一格回 exit 2 并让人"让它只回一个短 slug"。业主 2026-09-30 的输入是
    「创建一个1111文档」：Planner 回 name="1111文档" 是合理答案，拒掉就是
    白烧一次真实调用之后原地打转 —— 那是"怎么填都不行"里的第三道墙。
    文件名要 shell 友好这条判据还在，只是改成**折算**而不是拒绝。
    """

    def _plan(self, repo, supervisor_replies, capsys, name, ws=None):
        answer = plan_answer()
        answer["name"] = name
        supervisor_replies(answer)
        target = repo / "project.json"
        rc = bp.plan(target, "交付一个中文演示站点",
                     workspace=str(ws or repo), mock=True)
        return rc, target, capsys.readouterr().out

    def test_a_name_with_spaces_is_folded_and_still_written(
            self, repo, supervisor_replies, capsys):
        rc, target, out = self._plan(repo, supervisor_replies, capsys,
                                     "showcase-site — batch")
        assert rc == 0 and target.exists(), out
        spec = json.loads(target.read_text(encoding="utf-8"))
        assert bp.NAME_SLUG.match(spec["name"])
        assert spec["name"] == "showcase-site-batch"
        assert "已按" in out and "不影响交付" in out

    def test_a_chinese_name_keeps_its_ascii_part(self, repo,
                                                supervisor_replies, capsys):
        rc, target, out = self._plan(repo, supervisor_replies, capsys,
                                     "1111文档")
        assert rc == 0, out
        assert json.loads(target.read_text(
            encoding="utf-8"))["name"] == "1111"

    def test_a_pure_chinese_name_falls_back_to_the_project_slug(
            self, repo, supervisor_replies, capsys):
        blank = repo / "测试"
        blank.mkdir()
        rc, target, out = self._plan(repo, supervisor_replies, capsys,
                                     "文档项目", ws=blank)
        assert rc == 0, out
        assert json.loads(target.read_text(
            encoding="utf-8"))["name"] == "project"


class TestThePaidAnswerIsNotThrownAway:
    """校验不过时，**那次已经花掉额度的回答**要留在盘上。

    业主说"创建一个1111文档"，Planner 完全可能回一个任务 Plan 的形状、
    或某条 acceptance 写成中文描述 —— 那时旧行为是打一句"没有生成项目档"，
    原文跟着一起没了：人要再来一次，就得再付一次。
    """

    def test_a_rejected_answer_is_kept_next_to_the_project_file(
            self, repo, supervisor_replies, capsys):
        bad = {"name": "doc", "milestones": [
            {"id": "m1", "goal": "在工作区根新建 1111文档.md 并写清用途",
             "acceptance": "文档能打开就行"}]}
        supervisor_replies(bad)
        target = repo / "runtime_batch" / "p.project.json"
        assert bp.plan(target, "创建一个1111文档", workspace=str(repo),
                       mock=True) == 2
        out = capsys.readouterr().out
        kept = target.with_name("p.rejected.txt")
        assert "另存成" in out and kept.is_file()
        text = kept.read_text(encoding="utf-8")
        assert "文档能打开就行" in text, "原文要逐字留着，不是只留个摘要"
        assert "开头不是可执行名" in text, "拒绝的理由也要写在同一份文件里"

    def test_no_file_when_the_agent_never_answered(self, repo,
                                                   supervisor_replies,
                                                   capsys):
        supervisor_replies("")
        target = repo / "runtime_batch" / "q.project.json"
        assert bp.plan(target, "创建一个1111文档", workspace=str(repo),
                       mock=True) == 2
        assert not target.with_name("q.rejected.txt").exists()


class TestRecoverablePatch:
    """失败格 ≠ 白跑。批次拒绝跳步是对的，但要知道那一格有没有货。"""

    def _spec_state(self):
        spec = {"name": "p", "workspace": ".", "config_dir": "config",
                "milestones": [{"id": "m1", "goal": "建首页骨架与样式",
                                "acceptance": "pytest -q"},
                               {"id": "m2", "goal": "补关于页与导航链接",
                                "acceptance": "pytest -q"}]}
        state = {"milestones": {"m1": {"status": "failed",
                                       "runtime_task_id": "rt-f1"}},
                 "final": {"status": "not-run"}}
        return spec, state

    def _stub_collect(self, monkeypatch, patch_lines):
        from tools import delivery_view as dv

        monkeypatch.setattr(
            dv, "collect",
            lambda rt, cfg: ({"patch_lines": patch_lines}, ""))

    def test_a_failed_slice_with_a_patch_says_so(self, monkeypatch):
        self._stub_collect(monkeypatch, 227)
        spec, state = self._spec_state()
        hint = bp.recoverable_patch(spec, state)
        assert "227 行补丁" in hint and "rt-f1" in hint and "recheck" in hint

    def test_a_failed_slice_with_nothing_collected_stays_quiet(
            self, monkeypatch):
        self._stub_collect(monkeypatch, 0)
        spec, state = self._spec_state()
        assert bp.recoverable_patch(spec, state) == ""

    def test_no_failed_milestone_means_no_probe(self, monkeypatch):
        from tools import delivery_view as dv

        def boom(*a, **k):
            raise AssertionError("没有失败格就不该去采集")
        monkeypatch.setattr(dv, "collect", boom)
        spec, state = self._spec_state()
        state["milestones"]["m1"]["status"] = "done"
        assert bp.recoverable_patch(spec, state) == ""

    def test_unreadable_evidence_degrades_to_silence_not_a_guess(
            self, monkeypatch):
        from tools import delivery_view as dv

        def raise_(*a, **k):
            raise RuntimeError("库被搬走了")
        monkeypatch.setattr(dv, "collect", raise_)
        spec, state = self._spec_state()
        assert bp.recoverable_patch(spec, state) == ""

    def test_the_run_refusal_prints_the_hint(self, monkeypatch, capsys):
        self._stub_collect(monkeypatch, 227)
        spec, state = self._spec_state()
        assert bp.submit_next(spec, state, wait=False, serve=False) == 2
        out = capsys.readouterr().out
        assert "不会跳过" in out and "227 行补丁" in out


class TestDemoPreviewWiring:
    def test_run_demo_records_a_preview_when_asked(self, repo):
        from tools import demo_preview

        if not demo_preview.browser_path():
            pytest.skip("本机没有浏览器")
        (repo / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
        out = bp.run_demo({"id": "m1", "demo": {"command": [
            "python", "-c", "print('built')"]}}, str(repo),
            echo=lambda *a, **k: None,
            preview_dir=repo / "runtime_batch" / "prev")
        assert out["status"] == "ok"
        assert out["preview"]["status"] == "ok"
        assert any(s.endswith("index.png") for s in out["preview"]["shots"])

    def test_no_preview_dir_means_no_browser_is_spawning(self, repo):
        out = bp.run_demo({"id": "m1", "demo": {"command": [
            "python", "-c", "print(1)"]}}, str(repo),
            echo=lambda *a, **k: None)
        assert "preview" not in out
