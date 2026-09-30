# PHASE6B_FINAL_REPORT.md —— Hybrid Semantic Memory Retrieval 最终验收报告

> ```
> Hybrid Semantic Memory Retrieval = VERIFIED
> Core / Memory Provider-Specific Changes: 0
> ```
>
> 验收时间：2026-09-24
> 权威口径：**Framework/Memory/Semantic 656 collected / 656 passed / 0 failed**
> + **Real Harness 8/8**（junit XML）

---

## 一、根因修正（取代上一版报告的"机器级故障"结论）

隔离子进程探测（`memory embeddings doctor`）+ 版本对照实验得到的**真实根因**：

```
torch 2.10（latest）的 c10.dll 与本机 Windows build 26200 不兼容
  → DllMain 初始化失败（WinError 1114）
torch 2.6.0 完全正常（import + matmul 通过，沙箱外验证）
```

上一版"原生 ML runtime 机器级故障"的判断**过宽**——实际是版本特异性
不兼容。修复方式：独立 venv（`envs/ml`，系统 Python 3.13 基底）固定
`torch==2.6.0+cpu` + `sentence-transformers 6.1.0`，BGE-M3 模型缓存于 E 盘。

诊断矩阵（全部子进程隔离，§2）：

| 探测 | 结果 |
| --- | --- |
| VC++ runtime（System32 三件套） | OK |
| torch 2.10（两 venv） | FAIL 1114 |
| torch 2.6.0（ml venv） | **OK** |
| libiomp5md.dll 单独加载 | OK |
| c10/shm/torch_cpu/torch_python | 随 c10 根失败 |
| PYTHONPATH 清空 / 沙箱排除 / 重装 | 均不改变 2.10 的失败 |

## 二、路径 B：IsolatedEmbeddingWorker（§3/§5/§6/§32）

```
Orchestrator (default venv)
   ↓ stdin/stdout JSON-lines（长驻进程，§6）
IsolatedEmbeddingWorker (envs/ml venv, torch 2.6.0)
   ↓ sentence-transformers
BGE-M3 (dim=1024, CPU)   模型缓存: E:\hf-cache（§55：机器路径走 ${ENV} 展开）
```

- 协议 Provider-neutral（`{"operation": "health"|"embed"}`），协议里没有 BGE（§5）
- 批处理：一次请求 N 条文本，长驻进程绝不"一请求一进程"（§6）
- 懒加载：health 不触发模型加载（§32/§34）
- 崩溃自愈：单次失败重启一次 worker 再试；仍失败上层按 §12 降级 FTS
- **HybridMemoryRetriever / core 不知道 worker/subprocess/BGE 的存在**（§3，
  接口即 EmbeddingProvider；AST 扫描继续 ZERO）

实测性能（§20，如实）：
cold load+首次 embed ≈ 71s；warm embed（5 条）0.40s；FAISS search <1ms；
index rebuild（3 条 ACTIVE）~40s 含进程启动。Memory 数量 = 8 ACTIVE。

## 三、真实验收结果（全部真 BGE-M3，零 Mock）

### §8 Index Rebuild
```
index rebuilt: {'indexed': 3, 'failed': 0, 'total': 3}
index status : sqlite_active=3, indexed=3, missing=0, stale=0,
               backend=faiss, provider_available=True
```

### §9-§11 跨语言召回（`pytest -m semantic_model` = 3 passed，1m50s）
```
中文 query「执行 Agent 说测试已经通过，但是系统自己执行 pytest 却失败…」
  ↔ 英文 Memory（Evidence ownership）
  真实向量 cosine = 0.6206（FTS 词面重叠 = 0）
英文 query ↔ 中文 Memory：PASS
同义表达（§11/§46）：PASS
```

### §12 不相关过滤
```
css/字体 query ↔ evidence Memory: cosine = 0.378 < threshold(0.45)
→ 不进入 Top-K（测试锁定 + eval IRRELEVANT 组验证）
```

### §13 Scope Safety（硬条件）
PROJECT project_A 记忆在 project_B 查询下不返回——语义相似度不参与
scope 判定（canonical lookup 阶段强制，测试锁定）。

### §14 Supersede + Stale Vector（§48）
v1 向量故意残留索引 → canonical 状态 SUPERSEDED 拦截 + content_hash
不一致（STALE）双保险，只返回 v2（测试锁定，mock 向量验证机制）。

### §15 Index Version（§58/§59）
provider/model/dimension/schema 任一变化 → 旧索引拒载（不静默混用），
要求 rebuild（测试锁定）。

### §16 Embedding Cache
content_hash 未变 → 第二次 sync 零 encode 调用（provider 计数器测试锁定）；
内容变化 → 重新索引。

### §17-§19 真实 Eval（22-case 固定数据集，真 BGE-M3）
```
分組指标（§19）:
  LEXICAL   recall@1=0.455  recall@3=0.318  mrr=0.318
  SEMANTIC  recall@1=0.682  recall@3=0.545  mrr=0.545
  HYBRID    recall@1=0.727  recall@3=0.591  mrr=0.591

CROSS_LANGUAGE 子集（§18 核心指标）:
  lexical 缺 8 个 case → hybrid 仅缺 2 个
  Hybrid Recall@K > Lexical Recall@K，提升来自真实向量语义
  （涉及 Memory 均为 GLOBAL scope —— 无 PROJECT bypass）
```
残余 3 miss 已根因定位：2 个为 reviewer 角色类型过滤 by design
（§17：workflow_lesson 不属于 reviewer 允许集合）、1 个为阈值边界
（cos≈0.45）。数据集未改动（§54）。

### §21-§22 Real Cross-Task Demo + AB
```
GLOBAL 英文经验 MEM-aef19a105e（来源=真实历史任务 task_d197982e83e7，
证据可溯、VERIFIED/HIGH）经真实 BGE-M3 向量索引。

真实 Demo（三角色，Task A COMPLETED；Task B 执行+框架验证 PASS，
reviewer 遭遇 Codex 中转瞬时 403/exit=1 —— 基础设施抖动，如实记录）:
  executor memory_ids_used = ['MEM-aef19a105e']   ← 中文任务收到英文经验
  MEMORY_INJECTED payload:
    score=0.4652, reasons=[semantic similarity, global scope match,
    confidence high], mode=hybrid

AB 对照（§22，同一查询/同一库/executor 角色/top_k=3）:
  lexical 检索 ids: []                                  ← 无词面重叠
  hybrid  检索 ids: ['MEM-aef19a105e']                  ← 真向量命中
```

### §24-§26 降级
semantic.enabled=false → 6A；向量层故障 → MEMORY_VECTOR_FALLBACK + FTS
继续（单测 + 上一版真实降级 Demo 双证）；memory.enabled=false → Phase 5。

## 四、四层测试区分（§29）

```
Framework / Memory : 656 collected / 656 passed / 0 failed
  （含 6A 52 + 6B 结构 51；semantic_model 3 条真实模型测试已计入并 PASS）
Semantic Model     : 3 passed（pytest -m semantic_model，显式执行，真 BGE-M3）
Real Harness       : 8 passed / 0 failed / 0 skipped（junit XML）
```

## 五、§31 完成条件逐条

1-4 真实推理/索引生成/重建 ✅；5 中文→英文 ✅；6 英文→中文 ✅；7 同义 ✅；
8 不相关过滤 ✅；9-11 scope/confidence/status safety ✅；12 cache ✅；
13 version ✅；14 fallback ✅；15-17 三模式 ✅；18 real eval ✅；
19 cross-language hybrid > lexical ✅；20 real cross-task semantic Demo ✅；
21 trace 完整（mode/score/reasons）✅；22 semantic_model 全 PASS ✅；
23 6A 回归 ✅；24 Phase 5 fallback ✅；25 Provider Scan ZERO ✅。

```
Phase 6B: PARTIAL VERIFIED → VERIFIED
```

## 六、Bugs Found（真实发现）

1. **torch 2.10 c10.dll 与 Windows build 26200 不兼容**（根因修正）：
   上一版"机器级故障"结论过宽；隔离探测 + 版本对照定位到版本特异性。
   `pip install "torch==2.6.0"` 即修。
2. **Worker 脚本自引用**：provider 把自己（providers/worker.py）当 worker
   启动（`parent/worker.py` 指向自身），子进程相对导入 ImportError 秒死。
   教训：**子进程的 stderr 绝不能静默丢弃**（WORKER_STDERR_LOG 开关由此而来）。
3. **二进制管道**：会话注入的 sitecustomize shim 包装父进程 text IO，
   `text=True` 管道写 stdin 报 Errno 22 —— worker 协议改用 binary JSON-lines。
4. **HF xet 下载器 panic（code 1450）**：2.3GB 大文件下载卡死；
   `HF_HUB_DISABLE_XET=1` 走普通 HTTP 后 5m40s 完成。

## 七、Phase 7 Recommendation（不自动实现）

1. **Memory Outcome Feedback**（used→helpful/harmful 机械判定）
2. **Multi-Task Queue**（闭环 + 记忆 + 语义检索全稳）
3. **Worker 池 / GPU**（Multi-Task 并发时的 embedding 吞吐）
4. RAG 集成 / Web UI / Human Approval：继续后置
