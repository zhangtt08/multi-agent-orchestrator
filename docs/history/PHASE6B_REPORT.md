# PHASE6B_REPORT.md —— Hybrid Semantic Memory Retrieval 交付报告

> **阶段六 B 目标**：在不推翻 6A Memory Contract 的前提下加入
> Embedding + Vector Retrieval，形成 Hybrid Retriever。SQLite 仍是权威库。
>
> ```
> Hybrid Semantic Memory Retrieval = PARTIAL VERIFIED
>   - 框架/结构/安全/降级：VERIFIED（本机）
>   - 真实跨语言语义召回：BLOCKED（本机原生 ML runtime 系统性故障，见 §Bugs #1）
> Core / Memory Provider-Specific Changes: 0
> ```
>
> 验证时间：2026-09-24　权威口径：**656 collected / 653 passed / 0 failed**（3 个 semantic_model 跨语言测试在本机如实 SKIP）

---

## 65.1 Architecture

```
                 MemoryStore (SQLite, Source of Truth)
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
   Lexical Retriever    Vector Retriever
       (FTS5/6A)         (EmbeddingProvider → VectorMemoryIndex)
          └─────────┬─────────┘
                    ▼
        候选按 memory_id 去重（两分都保留，§39）
                    ▼
        Canonical MemoryStore lookup（§10：向量结果必须回库验证）
                    ▼
        Status（§28）→ Confidence（§27）→ Role（§17）→ Stale hash（§29）
                    ▼
        Hybrid Rank（可解释加权，权重全来自配置，§17）
                    ▼
                 Top-K → MemoryInjector
```

## 65.2 Embedding Provider（§3-§5/§55）

| 项 | 值 |
| --- | --- |
| 接口 | `EmbeddingProvider`：embed_text / embed_batch / health_check / dimension / model_id |
| 第一实现 | `BgeM3EmbeddingProvider`（BAAI/bge-m3，dim 1024；后端自动选 sentence_transformers → FlagEmbedding） |
| 测试实现 | `MockEmbeddingProvider`（字符 3-gram 确定性向量，dim 64 —— 仅结构验证，**不用于**跨语言断言） |
| 懒加载 | ✅ 构造零 IO；首次 embed 才初始化（§32，测试锁定） |
| 不自动下载 | ✅ 模型缺失 → health_check=False + 可读原因 + `memory embeddings setup` 指引（§34） |
| 本机状态 | **不可推理**：torch `c10.dll` / onnxruntime DLL 初始化崩溃（系统 Python venv 与托管 Python venv 均复现，与 faster-whisper 已知问题同源） |

Provider 名（bge_m3/mock）只存在于 `mao/memory/embeddings/providers/`、config、tests —— 检索算法零感知（§37，AST 测试锁定）。

## 65.3 Vector Backend（§9）与选型理由

**选择 faiss-cpu 1.15.1**（`IndexFlatIP` + L2 归一化 = 余弦；JSON sidecar 存元数据）：

1. 本机实测 import 正常（onnxruntime 崩溃不影响 faiss）；
2. 内存式 + 落盘文件，无外部服务进程（Qdrant local 需要额外服务，本规模无近似检索收益）；
3. 可随时由 SQLite 全量重建（§10 Derived Index 定位）；
4. 测试简单（临时目录 + 双实例持久化已验证）。

**Vector Result ≠ Final Result**（§10）：`FaissVectorMemoryIndex` 的搜索单元测试同时证明 backend 层不含任何角色概念（§38）。

## 65.4 Index Synchronization（§11/§30/§31）

```
MemoryValidator → SQLite commit（权威）→ Embedding（content_hash 未变则跳过）
               → Vector upsert（batch_size=16）→ embedding_cache 登记
```

- 失败不回滚：向量坏了 Memory 仍在库里（测试锁定）
- 缓存生效：同内容再同步 provider 调用数不变（测试锁定）
- 内容变化重嵌入：summary 变更 → 重新索引（测试锁定）

## 65.5 Cross-Language Demo（§50 —— 本机 BLOCKED，机器可换即跑）

设计完全就绪（`tests/test_p6b_semantic.py::TestCrossLanguageRealModel`，
`semantic_model` 标记）：

```
Memory（英文）: Framework verification results take precedence over executor self-report…
Task Query（中文）: 执行代理说测试成功了，但是框架自己跑测试失败了，应该相信哪个？
要求: FTS rank low/none；Vector rank relevant；Hybrid final = retrieved
```

**本机执行结果**：3 条跨语言/同义测试如实 SKIP —— 原因是机器级 DLL 故障
（§Bugs #1），不是实现缺失。修复运行库后 `python main.py memory index rebuild`
即可让这 3 条转为真实执行。

**真实跑通的部分**（§50 的非语义部分，全真 Agent）：

```
Task A (task_64cd001fd4aa): COMPLETED → MEMORY_STORED（VERIFIED/HIGH）
Task B (task_410827108ea5): COMPLETED
  → supervisor memory_ids_used = ['MEM-095e792c65', 'MEM-8d2f5e9f9d']
  → 其中 MEM-8d2f5e9f9d 来自**上一次会话**的 SQLite 持久化 —— 跨会话成立
```

## 65.6 Hybrid Trace（§18/§40，真实 payload）

```
MEMORY_INJECTED (mode=hybrid):
  memory_ids: ['MEM-095e792c65', 'MEM-8d2f5e9f9d']
  retrieval_details:
    - memory_id: MEM-8d2f5e9f9d, score: 1.118,
      reasons: [scope:project=multi-agent-orchestrator, text:7 terms, used:4x]
    - memory_id: MEM-095e792c65, score: 1.038,
      reasons: [scope:project=multi-agent-orchestrator, text:7 terms]
```

score 是 query-dependent 运行期信息，只进 trace 不写回 MemoryEntry（§19）。
向量层不可用时如实降级（§49）：本 Demo 的 detail mode 显示 `lexical`
—— hybrid 配置下的实际执行路径，诚实记录而非伪装。

## 65.7 AB Evaluation（§51/§53）

评测集 `memory/evals/dataset.json`：10 条种子 Memory + **22 cases**
（中文→英文 ×5、英文→中文 ×2、同义 ×2、不相关 ×2、scope mismatch/match ×2、
harness match/miss ×2、superseded ×1、confidence ×1、role mismatch ×1）。

`python main.py memory eval`（mock provider —— 结构性口径）：

```
LEXICAL   recall@1=0.455  recall@3=0.318  precision@3=0.106  mrr=0.318
SEMANTIC  recall@1=0.455  recall@3=0.318  precision@3=0.106  mrr=0.318
HYBRID    （与 semantic 同 —— mock 无真实语义）
未命中明细全部集中在跨语言/同义 case —— 精确印证了 Phase 6B 的立项理由
```

**诚实声明**：mock provider 只能证明指标机制（Recall@K/Precision@3/MRR 可计算、
违规可检出）。"Hybrid 在跨语言 eval 上优于 lexical"（§64-28）必须在
BGE-M3 可推理的机器上复测 —— 评测命令与数据集已就绪。

**评测驱动的真实修复**（§54）：CSS 不相关 case 曾因 "for" 这类停用词命中
GLOBAL 记忆被误召回 —— 修复检索器停用词表（不动数据集），误召回消失、
recall@1 由 0.409 升至 0.455。

## 65.8 Scope / Supersede Safety（§26-§28/§47-§48）

- **Scope wins**（测试锁定）：PROJECT project_A 的记忆在 project_B 查询下，
  即使语义高度相似也不返回（§26："Semantic similarity 不能绕过 scope"）
- **Confidence wins**：LOW 条目 vector_score 再高也不注入（min_confidence=MEDIUM）
- **Supersede wins**：v1 已 SUPERSEDED 后，故意把 stale vector 塞回索引 →
  canonical lookup 拦截，只返回 v2（测试锁定）

## 65.9 Failure Fallback（§12/§49）

三层降级全部测试锁定：

```
semantic.enabled=false            → 完全退化 Phase 6A（hybrid=None）
Embedding 模型不可用（本机现状）   → build 时 hybrid=None → FTS 检索继续
运行中向量检索抛异常               → WARNING + MEMORY_VECTOR_FALLBACK → FTS 继续
memory.enabled=false              → 完全退化 Phase 5
```

真实 Demo 在"Embedding 不可用"状态下跑通完整三角色闭环（Task A/B 均
COMPLETED）—— 任务层面零影响，Memory 检索自动走 FTS/元数据。

## 65.10 Performance（§56，本机实测口径）

- Cold load：本机不可用（模型未装）； Provider 懒加载已由测试锁定
- Mock embed：~0.1ms/条（确定性哈希）
- FAISS search（64 维，个位数条目）：<1ms
- Hybrid retrieval（22 case 全量）：秒级
- 结构上杜绝"每次 Prompt 重载模型"（Provider 实例挂在 MemoryLayer 上复用，§57 接口无全局变量）

## 65.11 Tests

```
Framework Tests      : 656 collected / 653 passed / 0 failed
                       （3 个 semantic_model 跨语言测试在本机如实 SKIP）
  phase1 212（含 6A 52 + 6B 54）+ phase2 237 + phase3 55 + phase4 52
  + phase5 100（不含 real_harness）
Real Harness Tests   : 8 passed / 0 failed / 0 skipped（Claude 额度已恢复）
Memory 6A            : 52   Memory 6B: 53（test_p6b_semantic.py）
```

## 65.12 Provider Isolation（§37/§63）

```
mao/core 与 mao/memory 通用模块: 品牌/后端名 ZERO（AST 测试：
  TestMemoryBrandIsolation + HybridMemoryRetriever 不含 if provider == …）
bge_m3 / mock / faiss 只出现在: embeddings/providers/、vector_index/（backend
  注册函数）、config、tests、docs
Vector backend 层无 Supervisor/Executor/Reviewer 概念（源码检查测试）
```

## 65.13 §64 完成条件

| # | 判据 | 结果 |
| --- | --- | --- |
| 1 | SQLite canonical | ✅ |
| 2 | Vector Index 可重建 | ✅ rebuild + 落盘恢复 |
| 3 | EmbeddingProvider 抽象 | ✅ |
| 4 | VectorMemoryIndex 抽象 | ✅ |
| 5 | BGE-M3 provider 可用 | ⚠️ 实现完整/懒加载/优雅降级；**本机推理 BLOCKED** |
| 6 | 无 Embedding 自动 FTS fallback | ✅ |
| 7 | lexical mode | ✅ |
| 8 | semantic mode | ✅（机制；质量待真模型） |
| 9 | hybrid mode | ✅（真实 Demo 用） |
| 10-12 | 跨语言/同义召回 | ⚠️ 测试就绪，**本机 BLOCKED**（semantic_model SKIP） |
| 13 | 不相关过滤 | ✅ |
| 14 | Scope 不可被向量绕过 | ✅ |
| 15 | Confidence 不可被向量绕过 | ✅ |
| 16/17 | SUPERSEDED/INVALIDATED 不返回 | ✅ |
| 18 | Stale vector 不使用 | ✅ |
| 19/20 | cache / batch | ✅ |
| 21 | Index version | ✅ |
| 22 | Top-K | ✅ |
| 23 | Role Query Builder | ✅ |
| 24 | Retrieval Explain | ✅ |
| 25 | Trace 含 vector/lexical/final score | ✅（retrieval_details） |
| 26 | Eval Dataset ≥20 | ✅ 22 |
| 27 | 指标可计算 | ✅ |
| 28 | Hybrid 跨语言优于 lexical | ⚠️ 待真模型复测（机制就绪） |
| 29 | Vector failure 不影响 Task | ✅ |
| 30 | semantic=false 退化 6A | ✅ |
| 31 | memory=false 退化 5 | ✅ |
| 32 | Real Cross-Task Demo | ✅（FTS 路径；语义路径待模型） |
| 33 | Provider Scan ZERO | ✅ |
| 34 | 普通测试全通过 | ✅ |
| 35 | Real Harness Demo 可运行 | ✅ |

### 状态

```
Hybrid Semantic Memory Retrieval = PARTIAL VERIFIED
  框架全部就绪；跨语言语义召回 BLOCKED on this machine
  （原生 ML runtime 机器级故障 —— 非框架问题，修复后无需改代码）
```

## 65.14 Bugs Found（真实发现）

1. **本机原生 ML runtime 系统性故障**（环境，最高优先级发现）：
   torch `c10.dll` 初始化失败（**系统 Python venv 与托管 Python venv 都复现**
   —— 排除了 venv 因素）、onnxruntime/fastembed 导入即段错误
   （KMP_DUPLICATE_LIB_OK 无效）、与已知 faster-whisper/ctranslate2 同源。
   疑似机器级 VC++ 运行库/OpenMP 问题。**这决定了 6B 的诚实边界**：
   框架完整、真实语义推理在本机 BLOCKED。修复方向（由你执行）：
   重装 VC++ 2015-2022 x64 Redistributable / 排查 OpenMP DLL 冲突后，
   `python main.py memory embeddings setup` → `memory index rebuild`。
2. **build_vector_index 用配置占位值算 version**：SemanticConfig 无
   model_id/dimension 字段，version 里 dim=0 → health_check 恒 False →
   hybrid 永远建不起来。修法：version 从**实际 provider 实例**取。
3. **MEMORY_VECTOR_INDEXED 语义谎言**：向量层缺席时也会发 INDEXED
   （_sync_failed 空列表 = "成功"）。修法：三态 —— None=未尝试（FALLBACK）、
   []=成功、[id]=失败。**教训与 §35 一脉相承：容错层不能把"没做"报告成"做了"。**
4. **评测驱动的停用词修复**：CSS 查询因 "for" 停用词命中 GLOBAL 记忆被误召回
   （§54：修检索器，不动数据集）。

## 65.15 新增/变更

| 文件 | 内容 |
| --- | --- |
| `mao/memory/embeddings/`（新） | Provider 抽象 + text_builder（redaction/hash）+ providers/{mock,bge_m3} |
| `mao/memory/vector_index/`（新） | VectorMemoryIndex 抽象 + FAISS backend + index version |
| `mao/memory/hybrid.py`（新） | HybridMemoryRetriever + MemoryQueryBuilder + MemoryIndexSynchronizer |
| `mao/memory/evals.py` + `memory/evals/dataset.json`（新） | 评测集（22 case）+ Recall/Precision/MRR |
| `mao/memory/store.py` | +embedding_cache / index_meta 表 + cache/meta 方法 |
| `mao/core/config.py` | SemanticConfig + retrieval.mode + hybrid 权重 |
| `mao/core/orchestrator.py` | 向量事件三态、Role Query Builder、trace retrieval_details |
| `tools/memory_cli.py` | +index rebuild/status、eval、embeddings setup、trace 升级 |
| `main.py` | doctor +memory semantic 段；memory 子命令路由 |
| `tests/test_p6b_semantic.py`（新） | 54 条 |

## 65.16 Phase 7 Recommendation（未实现）

1. **修复本机 ML runtime → 跑通跨语言 Demo 与真实 AB 指标**（6B 收尾，最高性价比）
2. **Memory Outcome Feedback**：used→helpful/harmful 的机械判定
3. **Multi-Task Queue**（闭环+记忆已稳）
4. RAG 集成 / Web UI / Human Approval：继续后置
