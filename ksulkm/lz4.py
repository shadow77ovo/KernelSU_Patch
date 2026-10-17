"""纯 Python 的 LZ4 解压（只解压，不压缩）。

Android 的 ramdisk / 内核常用 lz4，而标准库没有 lz4。
这里实现 LZ4 block / frame / legacy 三种格式的解码，仅用于“看懂镜像内容”
（判断是否已打补丁、读内核版本串），不参与实际修补。
"""

from __future__ import annotations

from typing import Optional

LZ4_FRAME_MAGIC = 0x184D2204
LZ4_LEGACY_MAGIC = 0x184C2102
LZ4_SKIPPABLE_MASK = 0xFFFFFFF0
LZ4_SKIPPABLE_MAGIC = 0x184D2A50


def _block_decompress(src: bytes, expected: int = 0) -> Optional[bytes]:
    """解压一个 LZ4 block（不带任何头部）。"""
    dst = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1

        lit_len = token >> 4
        if lit_len == 15:
            while i < n:
                b = src[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        if lit_len:
            if i + lit_len > n:
                return None
            dst += src[i : i + lit_len]
            i += lit_len

        if i >= n:
            break  # 最后一个序列只有字面量

        if i + 2 > n:
            return None
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(dst):
            return None

        match_len = token & 0x0F
        if match_len == 15:
            while i < n:
                b = src[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4

        start = len(dst) - offset
        # 注意：LZ4 允许重叠匹配，必须逐字节复制
        for k in range(match_len):
            dst.append(dst[start + k])

    if expected and len(dst) != expected:
        return None
    return bytes(dst)


def _frame(data: bytes, pos: int) -> tuple[Optional[bytes], int]:
    """解析一个 LZ4 frame，返回 (解压结果, 消耗的字节数)。"""
    n = len(data)
    if pos + 7 > n:
        return None, 0
    flg = data[pos + 4]
    bd = data[pos + 5]

    block_independent = bool(flg & 0x20)
    block_checksum = bool(flg & 0x10)
    has_content_size = bool(flg & 0x08)
    content_checksum = bool(flg & 0x04)
    has_dict_id = bool(flg & 0x01)

    p = pos + 6
    if has_content_size:
        p += 8
    if has_dict_id:
        p += 4
    p += 1  # header checksum

    block_max = {4: 64 << 10, 5: 256 << 10, 6: 1 << 20, 7: 4 << 20}.get((bd >> 4) & 0x07, 4 << 20)

    out = bytearray()
    prev = b""
    while p + 4 <= n:
        size = int.from_bytes(data[p : p + 4], "little")
        p += 4
        if size == 0:
            break  # end mark
        uncompressed = bool(size & 0x80000000)
        size &= 0x7FFFFFFF
        if p + size > n:
            return None, 0
        chunk = data[p : p + size]
        p += size

        if uncompressed:
            out += chunk
        else:
            base = b"" if block_independent else prev
            dec = _block_decompress(base + chunk)
            if dec is None:
                return None, 0
            out += dec[len(base) :] if base else dec
            prev = dec

        if block_checksum:
            p += 4

    if content_checksum:
        p += 4

    return bytes(out), p - pos


def _legacy(data: bytes, pos: int) -> tuple[Optional[bytes], int]:
    """解析 lz4 -l（legacy）格式：magic + 若干 [u32 长度][数据] 块。"""
    n = len(data)
    p = pos + 4
    out = bytearray()
    while p + 4 <= n:
        size = int.from_bytes(data[p : p + 4], "little")
        p += 4
        if size == 0 or p + size > n:
            break
        dec = _block_decompress(data[p : p + size])
        if dec is None:
            return None, 0
        out += dec
        p += size
    return bytes(out), p - pos


def decompress(data: bytes) -> Optional[bytes]:
    """解压 lz4 数据（自动识别 frame / legacy，支持多帧拼接）。"""
    if not data:
        return None
    out = bytearray()
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        magic = int.from_bytes(data[pos : pos + 4], "little")
        if magic == LZ4_FRAME_MAGIC:
            chunk, used = _frame(data, pos)
        elif magic == LZ4_LEGACY_MAGIC:
            chunk, used = _legacy(data, pos)
        elif (magic & LZ4_SKIPPABLE_MASK) == LZ4_SKIPPABLE_MAGIC:
            if pos + 8 > n:
                break
            skip = int.from_bytes(data[pos + 4 : pos + 8], "little")
            pos += 8 + skip
            continue
        else:
            break
        if chunk is None or used <= 0:
            return None
        out += chunk
        pos += used

    return bytes(out) if out else None
