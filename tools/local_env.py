"""本地凭据：`.env` 的读与写，以及"只报状态不报明文"的展示。

为什么要有这个模块：业主指出程序没有填 API key 的地方。查下来的根因不是界面上
少一个输入框，而是**仓库里根本没有读 `.env` 的代码** —— `.gitignore` 第 18 行
早就把 `.env` 排除了，`config/harness.yaml` 里的 `${SOME_KEY}` 全靠进程环境变量，
所以就算手写了这个文件也不会生效。先把这一层补上，界面才有意义。

安全边界（都是刻意的）：
- 只认仓库根下那一个 `.env`，路径不接受表单输入；
- 键名必须是 `[A-Z][A-Z0-9_]{1,63}`，值压成单行、去首尾空白；
- 对外只给"是否已设置 / 长度 / 末四位"，明文不出现在页面、日志或返回值里；
- 加载时**不覆盖**进程里已有的环境变量 —— 命令行显式 export 的优先级更高。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# 界面上给这几组一个说明位；不是白名单，别的键照样能填。
# 这一份必须与"代码/配置里真的会读的键"一致 —— 有一条测试逐个核对，
# 因为列一个程序不读的键，比不列更糟：那会让人以为填了就生效。
# 特别说明：**本程序不使用 API key**。额度来源是 CLI 的登录态
# （codex / claude 这些命令行工具自己已登录），要接一个走 API 的 provider，
# 得先在 config/harness.yaml 里加 profile 并用 ${VAR} 把 key 引进去。
SUGGESTED: List[Tuple[str, str]] = [
    ("CLAUDE_CLI_PATH", "Claude Code CLI 的可执行文件路径；留空走统一发现层"
                        "（PATH → 平台已知安装位置）"),
    ("CODEX_CLI_PATH", "Codex CLI 的可执行文件路径；同上"),
    ("HTTP_PROXY", "本机代理（github.com / api.openai.com 直连不通时要）"),
    ("HTTPS_PROXY", "同上，https"),
    ("MEMORY_EMBEDDING_INTERPRETER", "语义档：装了 torch 的独立 python 路径"),
    ("MEMORY_EMBEDDING_MODEL_PATH", "语义档：模型名或本地权重目录"),
    ("MEMORY_HF_HOME", "语义档：HuggingFace 缓存目录"),
    ("HF_ENDPOINT", "HuggingFace 端点（国内常填 hf-mirror.com）"),
    ("HF_HUB_DISABLE_XET", "语义档：关掉 xet 传输（1 即生效）"),
    ("ML_VENV_DIR", "tools/setup_embeddings.py 建 ML venv 的位置"),
    ("MAO_PY_EXE", "tools/baseline_count.py 跑测试用的 python"),
    ("MAO_MAX_ROUNDS", "覆盖配置里的轮数上限（整数）"),
    ("MAO_MAX_AGENT_CALLS", "覆盖每个任务的调用总量上限（整数）"),
    ("MAO_DRY_RUN", "1 = 干跑，不调用真实 CLI"),
    ("MAO_PREFLIGHT", "0 = 跳过跑前环境体检"),
    ("MAO_RUNTIME_DIR", "覆盖运行数据根目录"),
    ("MAO_HARNESS_FILE", "指定加载哪个 harness 文件"),
]

# 这一小组不由本程序读取，而是由我们拉起的第三方库/进程按标准约定读取。
# 单列出来是为了让守卫测试能区分两种"生效"：我们的代码读，或者我们启动的库读。
# 两者都不属于的键，不许出现在上面。
EXTERNAL_CONSUMED: Dict[str, str] = {
    "HTTP_PROXY": "requests / httpx / curl 等按标准约定读取，CLI 与模型下载走它",
    "HTTPS_PROXY": "同上（https）",
    "HF_ENDPOINT": "huggingface_hub 读取（国内镜像端点）",
    "HF_HUB_DISABLE_XET": "huggingface_hub 读取：关掉 xet 传输",
}


def env_path(root: Path) -> Path:
    return Path(root) / ".env"


def load_local_env(root: Path | str = ".") -> int:
    """把 `.env` 灌进进程环境（已存在的不覆盖）。返回写入的条数。"""
    p = env_path(Path(root))
    if not p.is_file():
        return 0
    applied = 0
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in raw.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not KEY_RE.match(key):
            continue
        if os.environ.get(key):          # 显式 export 的赢
            continue
        os.environ[key] = value
        applied += 1
    return applied


def read_all(root: Path | str = ".") -> Dict[str, str]:
    p = env_path(Path(root))
    out: Dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def set_key(key: str, value: str, root: Path | str = "."
            ) -> Tuple[bool, str]:
    """写一个键；value 为空串就是删除。返回 (是否成功, 给人看的话)。"""
    key = str(key or "").strip()
    # 不替用户大写：环境变量名差一个字符就是另一个变量。静默改写出来的键
    # 谁都读不到，那比拒收更糟。
    if not KEY_RE.match(key):
        return (False, "键名只能是大写字母、数字与下划线，且以字母开头。")
    value = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
    p = env_path(Path(root))
    lines: List[str] = []
    if p.is_file():
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [l for l in lines
            if not (LINE_RE.match(l.strip())
                    and LINE_RE.match(l.strip()).group(1) == key)]
    if value:
        kept.append(f"{key}={value}")
        os.environ[key] = value
        action = "已设置"
    else:
        os.environ.pop(key, None)
        action = "已删除"
    try:
        p.write_text("\n".join(kept) + ("\n" if kept else ""),
                     encoding="utf-8", newline="\n")
    except OSError as exc:
        return (False, f"写 {p} 失败：{exc}")
    return (True, f"{action} {key}（写进 .env，已被 gitignore，不会进版本库）")


def fingerprint(key: str, root: Path | str = ".") -> str:
    """给人看的状态：只说长度与末四位，永不出明文。"""
    value = str(os.environ.get(key) or read_all(root).get(key) or "")
    if not value:
        return "未设置"
    tail = value[-4:] if len(value) > 8 else "****"
    return f"已设置（长度 {len(value)}，末四位 {tail}）"
