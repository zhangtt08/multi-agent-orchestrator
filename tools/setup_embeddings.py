"""setup_embeddings —— 语义检索（BGE-M3）的一键可选安装，可重复执行。

    python tools/setup_embeddings.py            # 缺什么补什么，最后做真实健康校验
    python tools/setup_embeddings.py --check    # 只报告，不写任何东西

四步，每一步都是 detect → skip → verify（§14：已装好的绝不重来）：

```text
1. ML venv          独立虚拟环境（不污染主 venv，也不动主 venv）
2. torch + ST       按 requirements-ml.txt 的钉版安装
3. BGE-M3 模型       ~2.2GB，显式可选（--skip-model 跳过），已缓存则跳过
4. worker health     真的拉起 worker 并 embed 一次，作为唯一正证据
```

它做的写操作全部限制在项目目录内的 venv 与 HF 缓存里。明确不做（§12）：

```text
不改 PATH / 注册表 / shell profile
不 pip install 到全局或主 venv
不动系统里的 Git / CLI
不删任何已有文件
```

第 3 步是唯一的大流量操作，所以它只在**用户显式运行本脚本**时发生：
`tools/bootstrap.py` 和 `main.py doctor` 永远不会调用这里（§11）。

退出码：0 = 语义可用；1 = 未完成（见最后的状态表）；2 = 参数/配置用法错误。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_VENV = ".venv-ml"
DEFAULT_MODEL = "BAAI/bge-m3"
MODEL_SIZE_HINT = "约 2.2GB"

SKIP, PASS, FAIL, INFO = "SKIP", "PASS", "FAIL", "INFO"
_SYMBOL = {SKIP: "skip ", PASS: "ok   ", FAIL: "FAIL ", INFO: "info "}


def _row(kind: str, name: str, detail: str = "", action: str = "") -> Tuple[str, str]:
    text = f"  [{_SYMBOL[kind]}] {name:<20} {detail}".rstrip()
    if action and kind == FAIL:
        text += f"\n          → {action}"
    return kind, text


def venv_python(venv_dir: Path) -> Path:
    """venv 里的解释器路径（Windows / POSIX 两种布局）。"""
    win = venv_dir / "Scripts" / "python.exe"
    if win.exists():
        return win
    return venv_dir / "bin" / "python"


def _expand(value: str) -> str:
    from mao.harness.profiles import expand_env_placeholders

    text = expand_env_placeholders(str(value or ""))
    return "" if text.startswith("${") else text


def read_semantic(config_dir: str) -> dict:
    """从配置里取语义档的三个本机变量 + 下载端点。取不到就返回空。"""
    out = {"model_path": "", "hf_home": "", "hf_endpoint": "", "hf_extra_env": {}}
    try:
        from mao.core.config import load_config

        semantic = getattr(load_config(config_dir).settings.memory, "semantic", None)
    except Exception as exc:  # noqa: BLE001 - 配置坏了不该阻塞独立安装
        print(f"  (未能读取 {config_dir}/settings.yaml 的 semantic 段: "
              f"{type(exc).__name__}: {exc}；改用默认值)")
        return out
    if semantic is None:
        return out
    out["model_path"] = _expand(getattr(semantic, "model_path", "")) or DEFAULT_MODEL
    out["hf_home"] = _expand(getattr(semantic, "hf_home", ""))
    out["hf_endpoint"] = str(getattr(semantic, "hf_endpoint", "") or "")
    extra = getattr(semantic, "hf_extra_env", None) or {}
    out["hf_extra_env"] = {str(k): _expand(v) or str(v) for k, v in dict(extra).items()}
    return out


def model_cache_dir(model_path: str, hf_home: str) -> Optional[Path]:
    """模型在本机的落点：绝对路径直接用，HF repo id 映射到缓存目录。"""
    direct = Path(model_path)
    if direct.is_absolute() or direct.exists():
        return direct if direct.exists() else None
    hub_root = Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface"
    candidate = hub_root / "hub" / f"models--{model_path.replace('/', '--')}"
    return candidate if candidate.exists() else None


def worker_env(semantic: dict, hf_home: str) -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)          # 不继承会话级 shim 注入
    if hf_home:
        env["HF_HOME"] = str(hf_home)
    if semantic.get("hf_endpoint"):
        env["HF_ENDPOINT"] = semantic["hf_endpoint"]
    env.update({k: v for k, v in (semantic.get("hf_extra_env") or {}).items() if v})
    return {k: v for k, v in env.items()
            if k.startswith("HF_") or k in ("HOME", "USERPROFILE", "XDG_CACHE_HOME")}


# ---------------------------------------------------------------------------
# 步骤 1：ML venv
# ---------------------------------------------------------------------------
def step_venv(venv_dir: Path, base_python: str, check_only: bool) -> List[Tuple[str, str]]:
    exe = venv_python(venv_dir)
    if exe.exists():
        probe = subprocess.run([str(exe), "-c", "import sys;print(sys.version.split()[0])"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
        if probe.returncode == 0:
            return [_row(SKIP, "ml venv",
                         f"{venv_dir.name} 已存在 (python {probe.stdout.strip()})")]
        detail = (probe.stderr or "").strip().splitlines()
        return [_row(FAIL, "ml venv", f"{exe} 无法执行："
                                      f"{detail[-1][:120] if detail else '未知错误'}",
                     f"删掉 {venv_dir} 目录后重跑本脚本（脚本不会替你删）")]
    if check_only:
        return [_row(INFO, "ml venv", f"缺失 —— 将执行 {base_python} -m venv {venv_dir.name}")]
    created = subprocess.run([base_python, "-m", "venv", str(venv_dir)])
    if created.returncode != 0 or not venv_python(venv_dir).exists():
        return [_row(FAIL, "ml venv", f"创建失败（rc={created.returncode}）",
                     f"确认 {base_python} 可用且 {venv_dir} 父目录可写")]
    return [_row(PASS, "ml venv", str(venv_dir))]


# ---------------------------------------------------------------------------
# 步骤 2：torch + sentence-transformers（钉版，见 requirements-ml.txt）
# ---------------------------------------------------------------------------
def required_pins() -> dict:
    """解析 requirements-ml.txt 里的 `name==version` 钉版。"""
    pins = {}
    path = ROOT / "requirements-ml.txt"
    if not path.is_file():
        return pins
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if "==" in line:
            name, _, version = line.partition("==")
            pins[name.strip().lower()] = version.strip()
    return pins


def installed_versions(interpreter: str) -> dict:
    """在目标解释器里查 torch / sentence-transformers 的真实版本（子进程隔离）。

    torch 的 import 可能以 DLL 崩溃收场，那种失败不会是正常的 Python 异常，
    所以必须放在子进程里（与 tools/embeddings_doctor.py 同一纪律）。
    """
    code = (
        "import json\n"
        "out = {}\n"
        "for mod, dist in (('torch', 'torch'), ('sentence_transformers', 'sentence-transformers')):\n"
        "    try:\n"
        "        m = __import__(mod)\n"
        "        out[dist] = getattr(m, '__version__', 'unknown')\n"
        "    except Exception:\n"
        "        out[dist] = ''\n"
        "print(json.dumps(out))\n"
    )
    try:
        probe = subprocess.run([interpreter, "-c", code], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=300, env={**os.environ, "PYTHONPATH": ""},
                               cwd=str(Path(os.environ.get("TEMP", "."))))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if probe.returncode != 0:
        return {}
    import json

    try:
        return dict(json.loads((probe.stdout or "").strip().splitlines()[-1]))
    except (IndexError, ValueError):
        return {}


def step_deps(interpreter: str, check_only: bool, force: bool) -> List[Tuple[str, str]]:
    pins = required_pins()
    have = installed_versions(interpreter)
    missing = {k: v for k, v in pins.items()
               if not str(have.get(k, "")).startswith(v.split("+")[0])}
    if pins and not missing and not force:
        got = "  ".join(f"{k}={have[k]}" for k in pins)
        return [_row(SKIP, "ml deps", f"已满足钉版，不重装、不联网：{got}")]
    if check_only:
        what = ", ".join(f"{k}=={v}" for k, v in (missing or pins).items())
        return [_row(INFO, "ml deps", f"缺失/待装：{what or '（无）'}")]
    rows = [_row(INFO, "ml deps",
                 f"安装 {', '.join(f'{k}=={v}' for k, v in pins.items())}"
                 "（torch CPU 轮子，几分钟，输出直接透传）")]
    installed = subprocess.run(
        [interpreter, "-m", "pip", "install", "--no-input",
         "-r", str(ROOT / "requirements-ml.txt")])
    if installed.returncode != 0:
        rows.append(_row(FAIL, "ml deps", f"pip 退出码 {installed.returncode}",
                         f"多为网络/索引不可达。手动重试：\n"
                         f"     \"{interpreter}\" -m pip install -r requirements-ml.txt"))
        return rows
    still = {k: v for k, v in pins.items()
             if not str(installed_versions(interpreter).get(k, "")).startswith(v.split("+")[0])}
    if still:
        rows.append(_row(FAIL, "ml deps", f"装完仍不可 import：{list(still)}",
                         "Windows 上 torch 的 c10.dll WinError 1114 属于版本不兼容，"
                         "不要升级 —— 确认装的是 torch==2.6.0+cpu（见 docs/TROUBLESHOOTING.md）"))
        return rows
    rows.append(_row(PASS, "ml deps", "torch + sentence-transformers 可 import"))
    return rows


# ---------------------------------------------------------------------------
# 步骤 3：模型权重（显式可选，唯一大流量步骤）
# ---------------------------------------------------------------------------
def step_model(model_path: str, hf_home: str, interpreter: str, semantic: dict,
               check_only: bool, skip: bool) -> List[Tuple[str, str]]:
    cached = model_cache_dir(model_path, hf_home)
    if cached is not None:
        size_mb = sum(f.stat().st_size for f in cached.rglob("*")
                      if f.is_file()) // (1024 * 1024)
        return [_row(SKIP, "bge-m3 model", f"已缓存 {size_mb} MB：{cached}")]
    if skip:
        return [_row(INFO, "bge-m3 model", "--skip-model：不下载，语义检索继续走词法退化")]
    if check_only:
        return [_row(INFO, "bge-m3 model", f"未缓存 —— 将下载 {model_path}（{MODEL_SIZE_HINT}）")]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if hf_home:
        env["HF_HOME"] = str(hf_home)
    if semantic.get("hf_endpoint"):
        env["HF_ENDPOINT"] = semantic["hf_endpoint"]
    env.update(semantic.get("hf_extra_env") or {})

    from tools.embeddings_doctor import _probe_disk

    disk = _probe_disk(str(Path(hf_home).parent if hf_home else "C:\\"))
    print(f"  [info  ] 磁盘余量: {disk['detail']}")
    print(f"  [info  ] 下载 {model_path}（{MODEL_SIZE_HINT}）→ "
          f"{hf_home or Path.home() / '.cache' / 'huggingface'}")
    code = (
        "from huggingface_hub import snapshot_download\n"
        f"p = snapshot_download({model_path!r})\n"
        "print('cached at', p)\n"
    )
    # 输出透传：下载进度是用户此刻唯一想知道的事。
    result = subprocess.run([interpreter, "-c", code], env=env)
    if result.returncode != 0:
        endpoint = semantic.get("hf_endpoint") or "https://huggingface.co"
        return [_row(FAIL, "bge-m3 model", f"下载失败（rc={result.returncode}）",
                     f"检查网络；huggingface.co 不可达时设 HF_ENDPOINT={endpoint}")]
    if model_cache_dir(model_path, hf_home) is None:
        return [_row(FAIL, "bge-m3 model", "下载返回成功但找不到缓存目录",
                     f"HF_HOME 与本次探测不一致：hf_home={hf_home or '（默认）'}")]
    return [_row(PASS, "bge-m3 model", str(model_cache_dir(model_path, hf_home)))]


# ---------------------------------------------------------------------------
# 步骤 4：worker 真实健康校验（唯一正证据）
# ---------------------------------------------------------------------------
def step_health(interpreter: str, model_path: str, hf_home: str, semantic: dict,
                check_only: bool) -> List[Tuple[str, str]]:
    if check_only:
        return [_row(INFO, "worker health", "将拉起 worker 并真实 embed 一次")]
    if model_cache_dir(model_path, hf_home) is None:
        return [_row(FAIL, "worker health", "模型未就位，跳过拉起",
                     "先完成模型下载（去掉 --skip-model）")]
    from mao.memory.embeddings.providers.worker import WorkerEmbeddingProvider

    overrides = worker_env(semantic, hf_home)
    provider = WorkerEmbeddingProvider(
        interpreter=interpreter, model_path=model_path, device="cpu",
        timeout_seconds=900.0, env_overrides=overrides)
    if not provider.health_check():
        return [_row(FAIL, "worker health",
                     f"握手失败：{provider.available_reason[:160]}",
                     "worker 起不来通常是解释器/依赖问题，跑 "
                     "python main.py memory embeddings doctor 逐项定位")]
    rows = [_row(INFO, "worker health", "握手 OK，加载模型并 embed 一次（首次约 1-2 分钟）")]
    error = ""
    vectors: List[List[float]] = []
    try:
        vectors = provider.embed_batch(["semantic setup health check"])
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    provider.close()
    if vectors and vectors[0]:
        rows.insert(1, _row(PASS, "worker health",
                            f"真实嵌入成功 dimension={len(vectors[0])} "
                            f"(provider={provider.name})"))
    else:
        rows.append(_row(FAIL, "worker health", (error or "worker 返回空向量")[:160],
                         "模型加载失败：确认 torch 版本钉在 2.6.0+cpu，"
                         "细节看 python main.py memory embeddings doctor"))
    return rows


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup_embeddings",
        description="可选的语义检索安装：独立 ML venv + torch 2.6 + BGE-M3 + 健康校验")
    parser.add_argument("--config-dir", default="config",
                        help="读取哪份配置的 semantic 段（默认 config）")
    parser.add_argument("--venv-dir", default=os.environ.get("ML_VENV_DIR", DEFAULT_VENV),
                        help=f"ML venv 目录（默认 {DEFAULT_VENV}）")
    parser.add_argument("--python", dest="base_python", default=sys.executable,
                        help="创建 venv 用的基础解释器（默认当前 python）")
    parser.add_argument("--interpreter", default=None,
                        help="直接用这个已存在的 python 作为 ML 解释器，不创建 venv"
                             "（默认自动采用 MEMORY_EMBEDDING_INTERPRETER）")
    parser.add_argument("--model", default=None,
                        help=f"模型 id 或本地目录（默认取配置，回退 {DEFAULT_MODEL}）")
    parser.add_argument("--hf-home", default=None, help="HF 缓存目录（默认取配置）")
    parser.add_argument("--check", action="store_true",
                        help="只检测并打印计划，不写任何东西")
    parser.add_argument("--skip-model", action="store_true",
                        help="跳过模型下载（只要 venv + 依赖）")
    parser.add_argument("--skip-deps", action="store_true", help="跳过依赖安装")
    parser.add_argument("--force-deps", action="store_true",
                        help="即使版本已满足也重跑 pip install")
    args = parser.parse_args(None if argv is None else argv[1:])

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    semantic = read_semantic(args.config_dir)
    model_path = args.model or semantic["model_path"] or DEFAULT_MODEL
    hf_home = args.hf_home if args.hf_home is not None else semantic["hf_home"]
    venv_dir = (ROOT / args.venv_dir) if not Path(args.venv_dir).is_absolute() else Path(args.venv_dir)
    # 已经配好解释器的机器不该再被造出第二个 venv（§14：detect → skip）。
    configured = args.interpreter or _expand(os.environ.get("MEMORY_EMBEDDING_INTERPRETER", ""))
    reuse = configured if configured and Path(configured).is_file() else ""

    print()
    print("=" * 74)
    print(" 语义检索安装 —— Multi-Agent Orchestrator（可选组件）")
    print("=" * 74)
    print(f" 模型      : {model_path}（{MODEL_SIZE_HINT}）")
    print(f" HF 缓存   : {hf_home or Path.home() / '.cache' / 'huggingface'}")
    print(f" ML venv   : {reuse or venv_dir}")
    print(f" 模式      : {'只检测（--check，不写入）' if args.check else '检测后补齐缺失项'}")
    print("-" * 74)

    rows: List[Tuple[str, str]] = []
    if reuse:
        rows.append(_row(SKIP, "ml venv", "复用已存在的解释器，不另建 venv"))
        exe = Path(reuse)
    else:
        rows.extend(step_venv(venv_dir, args.base_python, args.check))
        exe = venv_python(venv_dir)
    runnable = exe.exists()
    if not runnable and not args.check:
        rows.append(_row(FAIL, "ml deps", "venv 不可用，跳过", "先修好上一步"))
        rows.append(_row(FAIL, "bge-m3 model", "venv 不可用，跳过", "先修好上一步"))
    else:
        if args.skip_deps:
            rows.append(_row(INFO, "ml deps", "--skip-deps：跳过"))
        else:
            rows.extend(step_deps(str(exe), args.check, args.force_deps))
        rows.extend(step_model(model_path, hf_home, str(exe), semantic,
                               args.check, args.skip_model))
        rows.extend(step_health(str(exe), model_path, hf_home, semantic, args.check))

    print("\n".join(text for _, text in rows))
    print("-" * 74)

    failed = [k for k, _ in rows if k == FAIL]
    if args.check:
        print("只检测模式：未改动任何文件。去掉 --check 即按上面的计划补齐。")
        return 0
    if failed:
        print("结论：语义检索未就位。按上面 FAIL 的 → 提示修完再重跑（可重复执行，")
        print("      已完成的步骤会 SKIP，不会重复下载）。期间检索退化为词法，")
        print("      Runtime 本身仍然可用。")
        return 1

    print("语义检索已就位。最后一步是把它接到主配置 —— 本脚本不改你的环境，")
    print("请把这三项写进 .env（或 shell 里 set），值如下：")
    print()
    print(f"  MEMORY_EMBEDDING_INTERPRETER={exe}")
    print(f"  MEMORY_EMBEDDING_MODEL_PATH={model_path}")
    print(f"  MEMORY_HF_HOME={hf_home or ''}")
    print()
    print("然后重建索引并确认模式：")
    print("  python main.py memory index rebuild")
    print("  python main.py doctor        # Memory Retrieval 应显示 HYBRID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
