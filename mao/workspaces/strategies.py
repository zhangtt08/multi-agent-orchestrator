"""WorkspaceStrategy（Phase 9 §9-§22）。

正式把 Phase 8 预留的三种策略落地：

    DIRECT        execution_workspace = source_workspace
                  同一 writable source 最多一个非终态 Task（§11，Phase 8 语义）
    GIT_WORKTREE  为 RuntimeTask 创建 detached worktree（§12）
                  —— 同一 source repo 的多个 Task 可并发（§21/§59/§60）
    COPY          非 git 项目的复制隔离（§20，带排除清单）

核心语义（§10）：RuntimeTask 区分
    source_workspace_path      只读起点
    execution_workspace_path   实际执行处
    base_revision              提交时钉住的 commit（§62）
    workspace_strategy         隔离策略

§15：Phase 9 只做"隔离执行 + 保存结果"，禁止自动 merge。
"""

from __future__ import annotations

from enum import Enum


class WorkspaceStrategy(str, Enum):
    DIRECT = "DIRECT"
    GIT_WORKTREE = "GIT_WORKTREE"
    COPY = "COPY"

    @classmethod
    def from_name(cls, name: str | None, default: "WorkspaceStrategy"
                  ) -> "WorkspaceStrategy":
        if not name:
            return default
        try:
            return cls(str(name).strip().upper())
        except ValueError:
            return default


# §20：COPY 默认排除清单（绝不复制数 GB 环境目录）
#
# 这一条在 2026-10-09 失守过一次，代价是磁盘上凭空多出来的 3.5 GB：
# 一条 `queue submit` 没带 `--workspace`（地雷 19 那条：空路径解析成进程 cwd =
# 仓库根），COPY 于是把**整个仓库**抄进 `runtime/example/runtime_workspaces/rt-…/`，
# 里面连 `.venv-ml`（1.47 GB 的 torch）都抄了 —— 清单里有 `.venv`，
# 但那是**手列的名字**，`.venv-ml` 不在列上就整份进去了。
# 所以这一版按**形状**（fnmatch 通配）写，而不是再往上补一个名字：
# 补名字就是下一个漂移。
#
# 已知取舍：`runtime*` 会把被复制项目里**自己**叫 runtime/ 的目录一起挡掉。
# 这一条不是新决定 —— 旧清单里的 `runtime` 早就这么做了（§20 的原意是
# "运行期目录不进副本"：副本是给 agent 干活用的，不需要上一批的证据目录，
# 而地雷 22 说过那些证据只有一份，抄一份出来只会变成第二份互相矛盾的现场）。
COPY_DEFAULT_EXCLUDES = (
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    "node_modules",
    ".venv*", "venv*", "envs",          # 虚拟环境：形状覆盖，别再数名字
    "runtime*",                          # 本框架与项目的运行期目录（含 *_batch/_scratch）
    "target-*", "target", "build-bin",   # 构建产物（cargo 的 gnu/msvc 两条路都在这）
    "dist-desktop",                      # 交付包本身是生成物
    "memory",
    ".acb",
)


__all__ = ["WorkspaceStrategy", "COPY_DEFAULT_EXCLUDES"]
