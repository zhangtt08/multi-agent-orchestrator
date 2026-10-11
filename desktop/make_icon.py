"""生成外壳图标：一个 512×512 的 PNG（纯标准库，不引 PIL）。

画的是这个产品的两件事：外面那一圈 = 执行 agent 在干活，里面那一圈 = 验收 agent
在对证据判定，中间那一点 = 业主写的那一句话。颜色沿旧页面那套（#0b5cad / #f08a24）。

用法：python desktop/make_icon.py            → desktop/icon.png
然后：cd desktop/src-tauri && npx tauri icon ../icon.png   （生成 icons/*.ico 等）
"""
from __future__ import annotations

import pathlib
import struct
import zlib

SIZE = 512
NAVY = (17, 24, 39, 255)
ORANGE = (240, 138, 36, 255)
BLUE = (11, 92, 173, 255)
WHITE = (248, 250, 252, 255)


def _px(x: int, y: int) -> tuple:
    cx, cy = SIZE / 2, SIZE / 2
    dx, dy = x - cx, y - cy
    r = (dx * dx + dy * dy) ** 0.5
    # 圆角方形底
    ax, ay = abs(dx), abs(dy)
    corner = 118
    inside = (ax <= cx - corner or ay <= cy - corner or
              ((ax - (cx - corner)) ** 2 + (ay - (cy - corner)) ** 2) <= corner ** 2)
    if not inside:
        return (0, 0, 0, 0)
    if 196 <= r <= 226:                      # 外圈：执行
        return ORANGE
    if 132 <= r <= 160:                      # 内圈：验收
        return BLUE
    if r <= 34:                              # 中心：那一句话
        return WHITE
    return NAVY


def chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def main() -> None:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)                                  # filter type 0
        for x in range(SIZE):
            raw.extend(_px(x, y))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    out = pathlib.Path(__file__).resolve().parent / "icon.png"
    out.write_bytes(png)
    print(f"写好 {out}  {len(png)} 字节  {SIZE}x{SIZE} RGBA")


if __name__ == "__main__":
    main()
