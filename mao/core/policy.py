"""ExecutionPolicy —— 角色能做什么，用配置说了算。

为什么按 Role 授权而不是按 Provider 授权
-----------------------------------------
如果写成"Codex 可以写文件、Claude 不行"，那么：

    换一个 Harness -> 权限模型要重写
    同一个 Harness 干两件事 -> 权限模型表达不了

而角色是框架自己的概念，天然稳定：

    supervisor: 只读（它产出的是方案，不是代码）
    executor:   可写工作区 + 可跑命令 + 可用 git
    reviewer:   只读代码 + 可跑有限的验收命令

这样换任何 Harness，权限模型都不动。这正是"可替换"在安全维度上的体现。

第二阶段不接 OS 级沙箱，但保留三件事：
  1. 模型（RolePolicy / ExecutionPolicy）
  2. 校验点（`check*` 系列方法，违规抛 PolicyViolationError）
  3. 记录（违规进日志，可被测试断言）

v1.9.18 起这一份策略**真的接在执行边界上**
-------------------------------------------
之前 `check_command()` 全仓没有任何调用方（唯一的调用点是 orchestrator 的
`check_workspace_write`），而 `allowed_commands` 默认是空列表 —— 空列表当时被读成
"什么都许"，于是整块策略在传输层是一件装饰。现在：

    SubprocessTransport.send_invocation() 在 Popen **之前**过一道 guard，
    guard 由 AgentRegistry 按角色从 `PolicyEnforcer.command_guard(role)` 注入。

判据仍然只有一份，就是本文件：

* `classify_command_shape()` —— 危险**形状**（唯一一份分类器）
* `PolicyEnforcer.check_command()` —— 角色 + 白名单 + 形状
* `PolicyEnforcer.command_guard()` —— 交给传输层的那个可调用对象

默认（`allowed_commands` 为空）不再是"什么都许"，而是"拦掉危险形状"；
`allowed_commands` 非空是**严格白名单**（并且形状地板仍然成立：把 `python` 加进
白名单不等于授权 `python -c`）。人类确实要全放开时写
`policy.roles.<role>.allow_any_command: true`（见 config/agents.yaml 的注释）。

为什么按**形状**而不是按子串
--------------------------
`sibling` 那个 hub 项目输在按原始子串判：`-fr` / `-rf` / `ri` / `rd` / `-enc`
换个写法就从指缝里过去了。这里先做逐 token 规范化（拆长短标志、折别名与前缀、
`--force-with-lease` 折算成 `force`），再判原子的**集合**。

为什么不碰 `VerificationRunner`
------------------------------
框架**代跑验收命令**那条路（`mao/verification.py` + `transports/process.run_once`）
有它自己一份更严的闸门：可执行文件必须在 `DEFAULT_ALLOWLIST` 里，不在就拒跑。
两条路边界的**覆盖面不同**是已知事实，写在这里而不是抹平：
传输层按形状拦（要能起任何 CLI，含 node/python），验收层按名字白名单拦
（所以 `python -c` 那一类在验收层被 `python` 这个名字放过、在传输层被形状拦下）。
把两边并成一个闸门要先决定"验收命令能不能用传输层那套默认"，那是产品决策。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import (Any, Callable, Dict, FrozenSet, Iterable, List, Optional,
                    Sequence, Set, Tuple)

from .exceptions import PolicyViolationError
from .models import ExecutionPolicy, Role, RolePolicy

# ---------------------------------------------------------------------------
# 形状判据（全仓唯一一份；别在第二个地方重拼这些词表）
# ---------------------------------------------------------------------------

_WINDOWS_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".vbs")


def _prefix_forms(words: Sequence[str]) -> Tuple[str, ...]:
    """长标志名的一切前缀写法（PowerShell 接受唯一前缀：-com -> -Command）。"""
    out: List[str] = []
    for word in words:
        for i in range(1, len(word) + 1):
            if word[:i] not in out:
                out.append(word[:i])
    return tuple(out)


_PS_INLINE_ATOMS: Tuple[str, ...] = (
    _prefix_forms(("command", "encodedcommand", "commandstring"))
    + ("c", "e", "command-string", "eval")
)

#: 这些可执行文件"把参数当代码读"的那些标志属于要拦的形状。键是**词干**，
#: 因为进到传输层的 argv[0] 已经被 `harness/discovery/executable.py` 换算成
#: 绝对路径（地雷 51：Windows 的 CreateProcess 按**调用方**的 PATH 找可执行文件，
#: 命令名必须在 argv 上就换算掉）。所以 `C:\\...\\python.exe` 与 `python`
#: 必须是同一条形状 —— 判据按词干，不按原始字符串。
#:
#: 值 = (内联标志原子, cluster)。`cluster=True` 表示短标志可以成组
#: （`-fr`、`-lc`、`-fdx`）：单个短横线的字母组要拆开判。
#: PowerShell / cmd / cscript 那一族的标志是**词**而不是字母组，cluster=False，
#: 否则 `-File` 会被拆成字母 e 而误判成 `-EncodedCommand` 的别名。
_INLINE_CODE_CLASS: Dict[str, Tuple[Tuple[str, ...], bool]] = {
    "python": (("c",), True),
    "python2": (("c",), True),
    "python3": (("c",), True),
    "py": (("c",), True),
    # node 的 -c 是 `--check`（语法检查），不是内联代码；-e / -p 才是。
    "node": (("e", "eval", "p", "print"), True),
    "deno": (("eval",), True),
    "perl": (("e", "eval"), True),
    "ruby": (("e", "eval"), True),
    "php": (("r", "run"), True),
    "osascript": (("e",), False),
    "cscript": (("e",), False),
    "wscript": (("e",), False),
    "java": (("e", "eval", "command-string"), True),
    "sh": (("c",), True),
    "bash": (("c",), True),
    "zsh": (("c",), True),
    "cmd": (("c", "k"), False),
    "powershell": (_PS_INLINE_ATOMS, False),
    "pwsh": (_PS_INLINE_ATOMS, False),
}

#: 任何被列进上面的解释器都逃不过这几个通用内联标志名。
_INLINE_CODE_GENERIC_ATOMS: FrozenSet[str] = frozenset(
    {"eval", "encodedcommand", "command-string", "commandstring"}
)

#: 递归删除的别名集合：PowerShell 里 `rm`/`del`/`erase`/`ri` 都是 Remove-Item，
#: cmd 里 `del`/`erase` 是它自己的。别名必须在**这里**展开，而不是靠原始子串，
#: 否则 `ri -Recurse -Force` 这一类躲得过。
_REMOVE_ITEM_ALIASES: FrozenSet[str] = frozenset(
    {"remove-item", "ri", "rm", "del", "erase"}
)
_RECURSE_ALIASES: FrozenSet[str] = frozenset({"recursive", "recurse", "r"})
_FORCE_ALIASES: FrozenSet[str] = frozenset({"force", "f"})

#: 长标志词表：出现在这里的名不再被拆成字母组（`-recursive` 不该产出 `f`）。
_LONG_FLAG_WORDS: Tuple[str, ...] = (
    "recursive", "recurse", "force", "interactive", "quiet", "silent",
    "hard", "file", "version", "nologo", "noprofile", "noninteractive",
    "noexit", "inputformat", "outputformat", "executionpolicy", "windowstyle",
    "configurationname", "workingdirectory", "outputtextwidth", "print",
    "eval", "run", "check", "help", "list", "format", "diskpart", "vssadmin",
)

#: 一句话就把环境毁掉的那些可执行文件（按词干判）。`dotnet format` 不在这一条里 ——
#: 它的 argv[0] 是 `dotnet`，那是代码格式化，不是磁盘格式化。
_ALWAYS_DENY: Dict[str, str] = {
    "format": "格式化磁盘（format）",
    "diskpart": "磁盘分区操作（diskpart）",
    "vssadmin": "卷影副本操作（vssadmin，删快照就是这一条）",
}

#: 接受"标志名唯一前缀"缩写的那一族（PowerShell 宿主与它的内建 cmdlet）。
_PREFIX_ALIAS_STEMS: FrozenSet[str] = frozenset(
    {"powershell", "pwsh", "remove-item", "ri", "del", "erase", "rd", "rmdir"}
)

#: git 的破坏性变体：子命令位置词 -> (必须命中的标志原子之一, 还要命中之一(可空), 形状名)
_GIT_SUBCOMMAND_RULES: Dict[str, Tuple[Set[str], Set[str], str]] = {
    "push": ({"f", "force"}, set(), "强推远端（git push --force / -f，含 --force-with-lease）"),
    "reset": ({"hard"}, set(), "硬重置工作区（git reset --hard）"),
    # `git clean -fd` 这一类：force 与 (d|x) 同时出现才拦 —— `-n` 预演与
    # "只删未跟踪文件但不进目录"不在本条要求里，不误伤。
    "clean": ({"f", "force"}, {"d", "x"}, "删除未跟踪文件与目录（git clean -fd / -fx）"),
}


def command_stem(command: Sequence[Any]) -> str:
    """argv[0] 的词干（去目录、去可执行文件后缀、小写）。"""
    if not command:
        return ""
    base = os.path.basename(str(command[0]).replace("\\", "/")).lower()
    stem, ext = os.path.splitext(base)
    if ext in _WINDOWS_SUFFIXES and stem:
        return stem
    return stem or base


def _strip_dashes(token: str) -> str:
    text = str(token).strip()
    while text and text[0] in "-/":
        text = text[1:]
    # cscript 的 //E:JScript、PowerShell 的 -Force:$true
    text = text.split(":", 1)[0]
    text = text.split("=", 1)[0]
    return text.strip().lower()


def _leading_dashes(token: str) -> int:
    count = 0
    for ch in str(token):
        if ch not in "-/":
            break
        count += 1
    return count


def _flag_atoms(token: str, *, cluster: bool, vocabulary: Sequence[str] = (),
                prefix: bool = False) -> Set[str]:
    """把一个参数规范化成"标志原子"集合。

    `-rf`                  -> {r, f}          成组短标志拆开
    `-Recurse`             -> {recurse}       词表里的长名不拆字母
    `--force-with-lease`   -> {force-with-lease, force}
    `/c` / `//E:JScript`   -> {c} / {e}

    `prefix=True` 才把唯一前缀折算成长名（PowerShell / cmdlet 那一族接受
    `-Rec` 这种缩写）。POSIX 工具**不接受**缩写，所以默认不开 —— 开在它们身上
    会把 `pip install -e .` 的 `-e` 折成 `eval` 而误拦（实测踩过一次，
    判据误报就是一条永久的墙）。
    """
    text = str(token)
    dashes = _leading_dashes(text)
    if not dashes:
        return set()
    name = _strip_dashes(text)
    if not name:
        return set()

    atoms: Set[str] = {name}
    head = name.split("-")[0]
    if head:
        atoms.add(head)
    for word in vocabulary:
        if name == word or name.startswith(word + "-"):
            # `--force-with-lease` 折算出 `force`。**不**用裸的 `name.startswith(word)`：
            # 那样 `-ExecutionPolicy` 会因为词表里有单字母 `e`（-EncodedCommand 的
            # 官方缩写）而被判成内联代码 —— 假阳性就是把判据变成一堵墙。
            atoms.add(word)
        elif prefix and word.startswith(name) and "-" not in name:
            # 唯一前缀写法（PowerShell 风格）：折算成词表里那一个长名。
            atoms.add(word)

    if cluster and dashes == 1 and name.isalpha() and len(name) > 1 and name not in vocabulary:
        # 成组短标志。两个短横线的 `--check` 是长名，绝不拆字母，
        # 否则它里面的 e 会被 node 的 `-e` 抓到。
        atoms.update(letter for letter in name)
    return atoms


def _positional_words(command: Sequence[Any]) -> List[str]:
    """非标志 token（按出现顺序）。git 的破坏性判据要先知道子命令是什么。"""
    words: List[str] = []
    skip_next = False
    for raw in list(command)[1:]:
        token = str(raw)
        if skip_next:
            skip_next = False
            continue
        if token and token[0] in "-/":
            name = _strip_dashes(token)
            # `-c user.name=t` 这种带值的短标志：值不是子命令。
            if "=" not in token and len(name) == 1:
                skip_next = True
            continue
        if token:
            words.append(token.lower())
    return words


def _inline_code_shape(stem: str, flags: Set[str],
                       per_token: Sequence[Tuple[str, Set[str]]]) -> Optional[str]:
    entry = _INLINE_CODE_CLASS.get(stem)
    if entry is None:
        return None
    hits = {a for a in entry[0] if a in flags} | (_INLINE_CODE_GENERIC_ATOMS & flags)
    if not hits:
        return None
    # 结论里要写**那一个原样 token**（`-enc` 而不是"命中了一堆前缀形式"），
    # 读的人才能据此行动（地雷 44）。
    return (f"{stem} 以内联代码方式执行（标志 {_offending_token(per_token, hits)}）"
            " —— 按解释器名加标志形状判，不看代码内容")


def _offending_token(per_token: Sequence[Tuple[str, Set[str]]],
                     hits: Set[str]) -> str:
    for token, atoms in per_token:
        if atoms & hits:
            return token
    return "/".join(sorted(hits))


def _destructive_shape(stem: str, flags: Set[str], words: Sequence[str],
                       per_token: Sequence[Tuple[str, Set[str]]]) -> Optional[str]:
    if stem in _ALWAYS_DENY:
        return _ALWAYS_DENY[stem]

    if stem == "cipher" and "w" in flags:
        return f"覆写空闲空间（{_offending_token(per_token, {'w'})}）"

    if stem in _REMOVE_ITEM_ALIASES:
        recursive = flags & _RECURSE_ALIASES
        force = flags & _FORCE_ALIASES
        if recursive and force:
            return (f"递归加强制删除（{stem} "
                    f"{_offending_token(per_token, recursive | force)} —— "
                    "-fr / -rf / -Recurse -Force 是同一类）")
        if stem in {"del", "erase"} and "s" in flags and (flags & {"f", "q"}):
            return (f"递归静默删除文件（{stem} "
                    f"{_offending_token(per_token, {'s', 'f', 'q'})}）")

    if stem in {"rd", "rmdir"} and "s" in flags and "q" in flags:
        return f"递归静默删除目录（{stem} {_offending_token(per_token, {'s', 'q'})}）"

    if stem == "git" and words:
        rule = _GIT_SUBCOMMAND_RULES.get(words[0])
        if rule is not None:
            required, also, label = rule
            if (flags & required) and (not also or (flags & also)):
                return f"{label}（git {words[0]} " \
                    f"{_offending_token(per_token, required | also)}）"
    return None


def classify_command_shape(command: Sequence[Any]) -> Optional[str]:
    """这条 argv 属不属于危险形状？属于就返回**这一条**形状的说法，否则 None。

    只做"形状"判定，不做角色判定（那是 `check_command` 的事），也不起任何进程。
    """
    argv = [str(t) for t in (command or [])]
    stem = command_stem(argv)
    if not stem:
        return None

    # 只有可执行文件、没有任何参数：只可能是"这个名字本身就危险"。
    if len(argv) < 2:
        return _ALWAYS_DENY.get(stem)

    entry = _INLINE_CODE_CLASS.get(stem)
    cluster = entry[1] if entry else True
    vocabulary: Tuple[str, ...] = _LONG_FLAG_WORDS
    if entry:
        vocabulary = tuple(entry[0]) + _LONG_FLAG_WORDS + tuple(_ALWAYS_DENY)

    per_token: List[Tuple[str, Set[str]]] = []
    flags: Set[str] = set()
    prefix = stem in _PREFIX_ALIAS_STEMS
    for token in argv[1:]:
        atoms = _flag_atoms(token, cluster=cluster, vocabulary=vocabulary,
                            prefix=prefix)
        if atoms:
            per_token.append((token, atoms))
            flags |= atoms

    words = _positional_words(argv)

    inline = _inline_code_shape(stem, flags, per_token)
    if inline:
        return inline
    if stem in {"deno", "node"} and "eval" in words:
        return f"{stem} eval <code> —— 内联代码执行"
    return _destructive_shape(stem, flags, words, per_token)


#: 拒绝时必须说清"真正走得通的出口"（本项目的规矩：说不出差在哪一条的判断
#: 不是判据，是墙 —— AGENTS.md 地雷 35/44/48）。
_SHAPE_REMEDY = (
    "走得通的替代：解释器改用脚本文件或 `-m 模块`（例如 `python -m pytest`）、"
    "PowerShell 用 `-File 脚本.ps1`、不要套一层 shell 去拼字符串；"
    "删除/强推这类动作交给人自己做，或在配置里显式放开这一档："
    "config/agents.yaml 写 `policy: {roles: {executor: {allow_any_command: true}}}` "
    "—— 它放开的是**所有**危险形状，只在受信目录里用。"
)

_ALLOWLIST_REMEDY = (
    "走得通的替代：把这条命令的名字加进 config/agents.yaml 的 "
    "`policy.roles.<role>.allowed_commands`（那是严格白名单），"
    "或让 Plan 改用白名单里已有的命令。"
)


class PolicyEnforcer:
    """ExecutionPolicy 的运行时执行者。

    刻意做成"检查 + 抛错"而不是"静默降级"：
    一个 Reviewer 试图写工作区是设计错误，必须响亮地失败，
    而不是悄悄把写操作变成空操作（那样会掩盖 bug）。
    """

    def __init__(self, policy: Optional[ExecutionPolicy] = None) -> None:
        self.policy = policy or ExecutionPolicy()
        self.violations: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def for_role(self, role: Role) -> RolePolicy:
        return self.policy.for_role(role)

    # ------------------------------------------------------------------
    def check_workspace_write(self, role: Role) -> None:
        if not self.for_role(role).workspace_write:
            self._violate(role, "workspace_write",
                          f"role {role.value!r} is not allowed to write the workspace")

    def check_shell(self, role: Role) -> None:
        if not self.for_role(role).shell:
            self._violate(role, "shell",
                          f"role {role.value!r} is not allowed to run commands")

    def check_git(self, role: Role) -> None:
        if not self.for_role(role).git:
            self._violate(role, "git",
                          f"role {role.value!r} is not allowed to use git")

    def check_command(self, role: Role, command: Sequence[str], *,
                      require_shell: bool = True) -> None:
        """检查单条命令：先看角色有没有 shell 权限，再看命令是否在授权范围内。

        `require_shell=False` 是给**框架自己起进程**那一条路用的（传输层的
        guard）：`RolePolicy.shell=False` 说的是"这个角色不许跑 shell 工具"，
        不是"这个角色不能被调用"。Supervisor 默认 shell=False ——
        要是连起它的 CLI 都过这条，三个角色一条命令都跑不起来，
        那不是收紧判据，那是造一堵墙（AGENTS.md 地雷 35）。
        """
        if require_shell:
            self.check_shell(role)
        role_policy = self.for_role(role)

        # 人明确要全放开的那一档：一条命令判据都不再问。
        if role_policy.allow_any_command:
            return

        shape = classify_command_shape(command)
        if shape:
            self._violate(
                role, "command_shape",
                f"执行策略拒绝起这条命令：{shape}。{_SHAPE_REMEDY}",
                command=[str(t) for t in list(command)[:4]],
                shape=shape,
            )

        allowed = role_policy.allowed_commands
        if not allowed:
            # 默认档：没有白名单，上面那道形状地板就是全部判据。
            return
        executable = command_stem(command)
        declared = {os.path.basename(str(a).replace("\\", "/")).lower() for a in allowed}
        declared |= {command_stem([a]) for a in allowed}
        declared.discard("")
        if executable not in declared:
            self._violate(
                role, "command",
                f"command {executable!r} is not in the role allowlist"
                f"（严格白名单：{sorted(declared)}）。{_ALLOWLIST_REMEDY}",
                command=[str(t) for t in list(command)[:4]],
                allowlist=sorted(declared),
            )

    def allows(self, role: Role, permission: str) -> bool:
        return bool(getattr(self.for_role(role), permission, False))

    def _violate(self, role: Role, permission: str, message: str,
                 **context: Any) -> None:
        record = {
            "role": role.value,
            "permission": permission,
            **context,
        }
        self.violations.append(record)
        # 注意：不能把 role/permission 作为额外的 **kwargs 传进去，
        # 否则会和显式关键字参数撞名（TypeError）。放进 extra 里。
        raise PolicyViolationError(
            message,
            role=role.value,
            permission=permission,
            extra=dict(context),
        )

    # ------------------------------------------------------------------
    # 传输层用得到的那个可调用对象
    # ------------------------------------------------------------------
    def command_guard(self, role: Role) -> "CommandGuard":
        """返回一个只吃 argv 的可调用对象（不认识角色之外的任何东西）。

        Transport 的规矩是"连 Role 都不该认识"（见 test_p2_adapter 的 §36 守卫），
        所以角色在这一层绑好，传下去的只是一个"argv -> 要么放行要么抛"的回调。
        """
        return CommandGuard(self, role)

    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            role.value: self.for_role(role).model_dump()
            for role in Role
        }

    def summary(self) -> str:
        """一行话说清当前生效的命令判据（doctor / 启动日志读这一份）。"""
        parts: List[str] = []
        for role in Role:
            role_policy = self.for_role(role)
            if role_policy.allow_any_command:
                state = "allow-any"
            elif role_policy.allowed_commands:
                state = f"allowlist={len(role_policy.allowed_commands)}+shape-floor"
            else:
                state = "shape-floor"
            parts.append(f"{role.value}={state}")
        return " ".join(parts)


@dataclass(frozen=True)
class CommandGuard:
    """绑好角色的命令闸门。

    frozen 是为了能当 `TransportRegistry` 缓存键的一部分：两个角色对同一个
    Transport 类有**不同**的判据，绝不能复用同一个实例（dry_run 当年就是
    栽在只按名字缓存上，见那里的注释 —— 同一个形状）。
    """

    enforcer: PolicyEnforcer
    role: Role

    def __call__(self, command: Sequence[Any]) -> None:
        self.enforcer.check_command(self.role, command, require_shell=False)

    @property
    def label(self) -> str:
        return f"{self.role.value}|{id(self.enforcer)}"


def shape_only_guard() -> Callable[[Sequence[Any]], None]:
    """没有角色信息时用的默认 guard：只上形状地板，不查白名单。

    `SubprocessTransport` 被**直接构造**（测试、tools 里的一次性冒烟）时走这一份，
    所以"危险形状"这条地板不依赖装配层有没有记得注入 —— 装饰性的默认值
    正是这一轮要消灭的东西。
    """

    def _guard(command: Sequence[Any]) -> None:
        shape = classify_command_shape(command)
        if shape:
            raise PolicyViolationError(
                f"执行策略拒绝起这条命令：{shape}。{_SHAPE_REMEDY}",
                permission="command_shape",
                extra={"shape": shape,
                       "command": [str(t) for t in list(command)[:4]]},
            )

    return _guard


def allow_all_command_guard() -> Callable[[Sequence[Any]], None]:
    """显式的"什么都放开"：调用方必须自己承担这个决定。

    测试里要跑 `python -c` 这类内联形状来验证**进程机制**（超时、取消、stdin 管道）
    时用这一份，而不是把生产默认放松。
    """

    def _guard(_command: Sequence[Any]) -> None:
        return None

    return _guard


def policy_from_config(raw: Optional[Dict[str, Any]]) -> ExecutionPolicy:
    """从 config 的 `policy:` 段构建策略。

    支持局部覆盖：只写想改的那一项，其余沿用默认值。

    两种写法：
        policy:
          roles:
            executor: {allowed_commands: [python, git]}
        policy:
          allow_any_command: true      # 三档一起放开（给人类的一条便捷写法）
    """
    if not raw:
        return ExecutionPolicy()

    base = ExecutionPolicy()
    roles: Dict[str, RolePolicy] = dict(base.roles)
    blanket = bool(raw.get("allow_any_command"))
    for role_name, override in (raw.get("roles") or {}).items():
        key = Role(role_name).value
        current = roles.get(key, RolePolicy())
        merged = current.model_dump()
        if isinstance(override, dict):
            merged.update({k: v for k, v in override.items() if k in merged})
        if blanket:
            merged["allow_any_command"] = True
        roles[key] = RolePolicy(**merged)
    if blanket:
        # 没在 roles 里出现过的角色也要放开（默认那三档都在，别的键是用户加的）。
        roles = {key: RolePolicy(**{**current.model_dump(),
                                    "allow_any_command": True})
                 for key, current in roles.items()}
    return ExecutionPolicy(roles=roles)


__all__ = [
    "CommandGuard",
    "PolicyEnforcer",
    "allow_all_command_guard",
    "classify_command_shape",
    "command_stem",
    "policy_from_config",
    "shape_only_guard",
]
