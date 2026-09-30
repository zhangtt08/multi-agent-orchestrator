"""mao —— multi-agent-orchestrator 的顶层包。

三个子包的职责与依赖方向：

    transports/  Agent 与真实 Harness 之间的通信抽象（无业务语义）
    core/        调度核心：模型 / 状态机 / Orchestrator / 持久化 / Prompt 加载
    agents/      Adapter 实现 + Agent 注册表

依赖方向严格单向：

    agents  ->  core  ->  transports

core 不认识任何具体 provider。唯一同时认识 core 与 agents 的地方是
mao.bootstrap（装配层）。

子包不做 eager import —— 调用方按需导入：

    from mao.core import Orchestrator
    from mao.bootstrap import build_orchestrator
"""

__version__ = "1.9.17"
"""发布版本。

与项目根的 `VERSION` 文件必须一致 —— 这条由
`tests/test_release_boundaries.py` 断言，不靠人记。
（刻意不去运行时读文件：staging / zip 分发里 `VERSION` 在包目录之外，
 按相对路径读会在打包边界上出岔子。）
"""

__all__ = ["core", "agents", "transports", "bootstrap", "__version__"]
