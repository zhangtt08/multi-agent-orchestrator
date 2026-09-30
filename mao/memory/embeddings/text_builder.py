"""Embedding 文本构造与 content hash（阶段六 B §6/§7/§44/§58）。

为什么单独一层：向量索引里存的不是 Memory，而是 Memory 的**语义投影**。
投影口径一变，旧向量就整体作废，所以这个口径必须满足三件事：

1. 只包含**经验正文**（§6）。memory_id / created_at / use_count / source_task_id
   这类运行时字段一旦进入文本，同一条经验会因为"被用过几次""来自哪个任务"
   而改变向量 —— 检索于是开始追逐记账信息而不是内容，而且永远无法复现。
   memory_type / tags / scope / confidence 同样不进文本，但理由是另一条：
   它们是**过滤条件**，全库每一条都带同样的几个词，进文本就等于给所有
   经验之间垫一个常数相似度底噪，把 §25 要防的"都是软件开发所以都相似"
   从检索层挪进向量层。过滤由 `hybrid.py` 在 canonical store 上机械判定。
2. 带 schema 版本（§58）。字段口径改动必须让 content_hash 变化，
   从而让 index_version 失配、旧索引被拒载，而不是两套口径静默混用。
   SCHEMA_VERSION 走**类属性**读取（不是实例快照），因此调整版本立即生效。
3. 落盘前再脱敏一次（§44）。Validator 挡住密钥是第一道；送进 embedding 的
   文本是**离开本进程**的那份，独立再挡一次才叫 defense in depth。

脱敏的"密钥形状"判据复用 `mao.core.logging_setup.redact_text()`，
不在这里写第二套 —— 两套判据迟早漂移（AGENTS「判据归属」）。
路径类信息是 embedding 特有的泄漏面：Memory 的 summary 里经常引用真实
文件路径，那会随向量进入索引文件与 worker 子进程，所以这里额外补一条。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import List

from ...core.logging_setup import redact_text
from ..models import MemoryEntry

_REDACTED = "[REDACTED]"

#: 机器归属：Windows 用户目录下的绝对路径（只抹掉到用户名那一层，
#: 保留后面的相对部分 —— 相对路径不带机器归属信息，留着才有诊断价值）
_WIN_USER_PATH = re.compile(r"[A-Za-z]:[\\/](?:Users|users)[\\/][^\\/\s\"',;)}\]]+")
#: 机器归属：POSIX 家目录（含 WSL 挂载点）
_POSIX_HOME_PATH = re.compile(r"/(?:home|Users|mnt/[Cc]/Users)/[^/\s\"',;)}\]]+")
#: 机器归属：`~/x` 与 `\\server\share` 形式
_TILDE_PATH = re.compile(r"(?<=\s)~[\\/][^\\/\s\"',;)}\]]*")
_UNC_PATH = re.compile(r"\\\\[^\\/\s\"',;)}\]]+[\\/][^\\/\s\"',;)}\]]+")

#: 语义字段（§6）。顺序固定 —— 文本进 hash，拼接顺序变化等于 schema 变化。
_SEMANTIC_SLOTS: tuple = (
    "problem_pattern", "solution_pattern", "failure_pattern",
)


def redact_for_embedding(text: str) -> str:
    """抹掉疑似密钥与机器本地路径，返回可送进 embedding 的文本。

    密钥形状交给 `redact_text()`（框架唯一一份判据）；路径在这里补，
    因为日志脱敏不处理路径 —— 日志留在本机，向量却会离开本机。
    """
    if not text:
        return text
    out = redact_text(text)
    for pattern in (_WIN_USER_PATH, _POSIX_HOME_PATH, _UNC_PATH, _TILDE_PATH):
        out = pattern.sub(_REDACTED, out)
    home = os.path.expanduser("~")
    if home and home not in ("~", "\\") and home in out:
        out = out.replace(home, _REDACTED)
    return out


class MemoryEmbeddingTextBuilder:
    """MemoryEntry -> 稳定文本 -> content_hash（§6/§7/§58）。"""

    #: 文本口径版本。加字段、改分隔符、改排序规则都要 +1 ——
    #: index_version 里带着它，旧索引于是自动失效而不是静默混用。
    SCHEMA_VERSION = 1

    def build_text(self, entry: MemoryEntry) -> str:
        """拼**经验正文**。不含 memory_id / 时间戳 / use_count / 来源任务（§6）。

        也不含 memory_type / tags / scope / confidence —— 这些是**过滤用**的
        元数据，不是内容：全库每一条都带 `workflow_lesson`、`tags`、`scope`
        这些词的话，任何两条经验的向量都会共享一个常数底噪，§25 想防的
        "都是软件开发所以都相似"就从检索层挪进了向量层。
        过滤条件由 `hybrid.py` 从 canonical store 机械判定，不需要向量替它说话。
        """
        lines: List[str] = [
            (entry.title or "").strip(),
            (entry.summary or "").strip(),
        ]
        for attr in _SEMANTIC_SLOTS:
            value = str(getattr(entry, attr, "") or "").strip()
            if value:
                lines.append(value)
        return redact_for_embedding("\n".join(line for line in lines if line))

    def content_hash(self, entry: MemoryEntry) -> str:
        """语义内容的指纹（§7/§30）。cache 命中判定与 stale 检测共用它。

        schema 版本进 hash：口径变了就必须让每条经验都"看起来变了"，
        否则旧向量会带着新口径的标签继续被采信（§58）。
        """
        payload = f"schema={self.SCHEMA_VERSION}\n{self.build_text(entry)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["MemoryEmbeddingTextBuilder", "redact_for_embedding"]
