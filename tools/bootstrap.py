"""bootstrap —— 一条命令判断"这台机器能不能跑"，并且只给建议不动系统。

    python tools/bootstrap.py
    python tools/bootstrap.py --config-dir config
    python tools/bootstrap.py --offline        # 离线档：Mock provider，不需要任何 CLI

判据与 `python main.py doctor` 完全同源（tools/env_report.py）。两者只是结论
呈现方式不同：doctor 是产品第一入口，bootstrap 是"装完之后第一次自检"。
曾经它们各查一套，同一件事给出两个答案 —— 那比查不出来更糟。

它做的全部事情都是读取与检查，外加一件事：按需创建软件自己的数据区
（runtime / memory / queue / checkpoint 的目录与 SQLite schema）。

明确不做（这是纪律，不是遗漏）：

```text
不改 PATH / 注册表 / shell profile
不 pip install（全局或隐式）
不安装 Git / CLI / 模型
不删任何文件
不下载大模型（约 2.2GB 的 BGE-M3 是显式决定，见 tools/setup_embeddings.py）
不调用真实 Agent CLI（那会烧配额；要确认真实链路用 tools/smoke_real_harness.py）
```

退出码：0 = 可以跑；1 = 有阻塞项；2 = 连检查都没法完成。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap", description=__doc__.splitlines()[0])
    parser.add_argument("--config-dir", default="config",
                        help="要检查哪份配置（默认生产配置 config/）")
    parser.add_argument("--offline", action="store_true",
                        help="按离线档检查（archive/config-history/config_offline，不需要任何真实 CLI）")
    args = parser.parse_args(None if argv is None else argv[1:])

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    from tools.env_report import FAIL, OK, WARN, collect

    config_dir = "archive/config-history/config_offline" if args.offline else args.config_dir
    report = collect(config_dir)
    worst = report.worst()

    print()
    print("=" * 74)
    print(f" bootstrap —— Multi-Agent Orchestrator  (config: {config_dir})")
    print("=" * 74)
    print()
    print(report.render())
    print("-" * 74)
    print(f" 合计: {report.counts()}")

    actions = report.actions()
    if actions:
        print("\n 下一步：")
        for line in actions:
            print(f"   • {line}")

    print()
    if worst == OK:
        print("结论：可以跑。下一步：")
        print("  python tools/smoke_test.py            # 不烧配额的端到端自检")
        print("  python main.py queue submit --goal \"...\" --workspace <目录>")
        print("  python main.py scheduler run")
    elif worst == WARN:
        print("结论：可以跑，但有降级项（见上面 WARN 的 → 提示）。")
    else:
        print("结论：有阻塞项。按上面每条 → 提示修完再跑 bootstrap。")
    print("本工具不安装任何东西、不改 PATH/注册表、不下载模型、不删文件。")

    if not report.items:
        return 2                      # 连一条检查都没跑起来
    return 0 if worst != FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
