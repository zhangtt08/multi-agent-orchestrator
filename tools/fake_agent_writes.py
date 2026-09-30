"""写文件的假 agent —— 让零配额这一跑能产出**真的 git diff**。

tests/fake_cli_agent.py 已经会按角色吐出合法 JSON（executor 三轮 FAIL→FAIL→PASS、
reviewer 判、supervisor 出计划），但它不写文件；框架的改动取证走的是
`git diff`，所以没有真改动就没有补丁，无人值守的闸门就会（正确地）拒绝合入。
这一层只补一件事：先把文件写进 cwd（隔离工作区），再把原样交给 fake_cli_agent 回答。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_cli_agent.py"

FILES = {
    "src/components/Modal.tsx":
        "export function Modal() {\n  // ESC closes and unmounts cleanly\n"
        "  return null;\n}\n",
    "src/hooks/useEscapeKey.ts":
        "export function useEscapeKey(onClose: () => void) {\n  return onClose;\n}\n",
    "src/navigation/closeFlow.ts":
        "// round 3: route is updated together with the modal close\n"
        "export const closeFlow = () => {};\n",
}


def main() -> int:
    data = sys.stdin.buffer.read()          # 提示词原样转发，不改一个字
    for rel, body in FILES.items():
        p = Path.cwd() / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8", newline="\n")
    proc = subprocess.run([sys.executable, str(FAKE)] + sys.argv[1:],
                          input=data, capture_output=True)
    sys.stdout.buffer.write(proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
