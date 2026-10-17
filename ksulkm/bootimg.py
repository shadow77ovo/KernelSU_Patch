"""Android 引导镜像头解析（只读预检）。

这里只做“看懂”镜像这件事，不做任何修改：真正的解包/改 ramdisk/重打包全部
交给官方 ksud 引擎，避免自己实现 boot image / AVB / 压缩格式带来风险。

解析范围：
  * Android boot image  v0 ~ v4（重点 v3 / v4，这也是 LKM 模式支持的范围）
  * init_boot.img       （v4、无内核、只有 ramdisk）
  * vendor_boot.img     v3 / v4（含 v4 的 ramdisk 分片表，可定位 init_boot 分片）
"""

from __future__ import annotations

import bz2
import lzma
import mmap
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import lz4
from .util import human_size, sha256_file

BOOT_MAGIC = b"ANDROID!"
VENDOR_MAGIC = b"VNDRBOOT"
AVB_FOOTER_MAGIC = b"AVBf"

# 各压缩格式的头部魔数，用于“猜”ramdisk 用的什么压缩
COMPRESSION_MAGICS = [
    (b"\x1f\x8b\x08", "gzip"),
    (b"\x04\x22\x4d\x18", "lz4"),
    (b"\x02\x21\x4c\x18", "lz4_legacy"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x5d\x00\x00", "lzma"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"BZh", "bzip2"),
    (b"\x89LZO", "lzo"),
]

# 与 ksud 的 parse_kmi 保持同一套启发式：在内核里找
#   "5.15.94-android13-8-gxxxx"  ->  android13-5.15
KMI_RE = re.compile(rb"(?<![0-9])([5-9]\.\d+)[^\x00\s]{0,96}?(android\d+)")
LINUX_VERSION_RE = re.compile(rb"Linux version ([^\x00\n]{8,200})")

def _align(value: int, align: int) -> int:
    if align <= 0:
        return value
    return (value + align - 1) // align * align


def _u32(buf, off: int) -> int:
    if off + 4 > len(buf):
        return 0
    return int.from_bytes(buf[off : off + 4], "little")


def _u64(buf, off: int) -> int:
    if off + 8 > len(buf):
        return 0
    return int.from_bytes(buf[off : off + 8], "little")


def _cstr(buf, off: int, length: int) -> str:
    raw = bytes(buf[off : off + length])
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def detect_compression(data: bytes) -> str:
    if not data:
        return "empty"
    for magic, name in COMPRESSION_MAGICS:
        if data.startswith(magic):
            return name
    return "raw"


def decompress(data: bytes, max_size: int = 256 << 20) -> tuple[Optional[bytes], str]:
    """尽力解压，仅用于“看懂内容”（找内核版本串 / 判断是否已打补丁）。

    不支持 zstd / lz4（标准库没有），返回 (None, 格式名) 交由上层降级处理。
    """
    if not data:
        return None, "empty"
    kind = detect_compression(data)

    def _try(func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            return None

    if kind == "gzip":
        obj = _try(zlib.decompressobj, 16 + zlib.MAX_WBITS)
        if obj is not None:
            out = _try(obj.decompress, data, max_size)
            if out is not None:
                return out, kind
        return None, kind
    if kind == "xz":
        return _try(lzma.decompress, data, format=lzma.FORMAT_XZ), kind
    if kind == "lzma":
        return _try(lzma.decompress, data, format=lzma.FORMAT_ALONE), kind
    if kind == "bzip2":
        return _try(bz2.decompress, data), kind
    if kind in ("lz4", "lz4_legacy"):
        return _try(lz4.decompress, data), kind
    if kind == "raw":
        return data, kind
    return None, kind


@dataclass
class BootImageInfo:
    """镜像的“体检报告”。"""

    path: Path
    size: int
    kind: str = "unknown"          # boot | init_boot | vendor_boot | unknown
    version: int = -1              # 头部版本，-1 表示未知
    page_size: int = 0
    kernel_size: int = 0
    ramdisk_size: int = 0
    ramdisk_offset: int = 0
    ramdisk_compression: str = "unknown"
    kernel_version: str = ""       # 例如 5.15.94-android13-8-g1a2b3c
    kmi: str = ""                  # 例如 android13-5.15
    avb: bool = False
    already_patched: Optional[bool] = None
    ramdisk_entries: list[str] = field(default_factory=list)
    ksu_config: str = ""
    has_force_debuggable: bool = False
    sha256: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    # ---- 给界面用的展示文本 ----
    @property
    def kind_label(self) -> str:
        return {
            "boot": "boot 镜像",
            "init_boot": "init_boot 镜像",
            "vendor_boot": "vendor_boot 镜像",
        }.get(self.kind, "未知格式")

    @property
    def version_label(self) -> str:
        return f"v{self.version}" if self.version >= 0 else "-"

    @property
    def has_ramdisk(self) -> bool:
        return self.ramdisk_size > 0

    @property
    def supported_by_engine(self) -> bool:
        """官方 ksud 的 boot-patch 只接受 header v3 及以上。"""
        if self.kind == "unknown":
            return False
        if self.kind == "vendor_boot":
            return self.version >= 3
        return self.version >= 3

    def summary_lines(self) -> list[str]:
        lines = [
            f"格式：{self.kind_label} {self.version_label}    页大小：{self.page_size or '-'}",
            f"内核：{self.kernel_version or '无（init_boot 不含内核）'}"
            f"    KMI：{self.kmi or '无法自动识别'}",
            f"ramdisk：{human_size(self.ramdisk_size) if self.ramdisk_size else '无'}"
            f"（{self.ramdisk_compression}）",
            f"AVB：{'存在签名尾（解锁 bootloader 后刷入不受影响）' if self.avb else '未检测到'}",
        ]
        if self.already_patched is True:
            lines.append("状态：ramdisk 内已存在 kernelsu.ko，可能已打过补丁")
        return lines


def _parse_kernel_meta(kernel: bytes) -> tuple[str, str]:
    """从内核数据里推断 (内核版本串, KMI)。"""
    if not kernel:
        return "", ""
    data, _ = decompress(kernel)
    if data is None:
        data = kernel  # 压缩格式不支持时直接搜原始字节（未压缩内核很常见）

    version = ""
    m = LINUX_VERSION_RE.search(data)
    if m:
        # 只要第一段（内核版本号 + 厂商后缀），后面是编译器信息，展示时没意义
        version = m.group(1).decode("utf-8", "replace").strip().split(" ")[0]
        if len(version) > 72:
            version = version[:71] + "…"

    kmi = ""
    m2 = KMI_RE.search(data)
    if m2:
        kernel_major_minor = m2.group(1).decode()
        android = m2.group(2).decode()
        kmi = f"{android}-{kernel_major_minor}"
    return version, kmi


def _vendor_ramdisk_table(buf, header_off: int, version: int) -> list[tuple[str, int, int]]:
    """解析 vendor_boot v4 的 ramdisk 分片表，返回 [(name, offset, size)]。"""
    if version < 4:
        return []
    page = _u32(buf, header_off + 12)
    header_size = _u32(buf, header_off + 2096)
    vendor_ramdisk_size = _u32(buf, header_off + 24)
    dtb_size = _u32(buf, header_off + 2100)
    table_size = _u32(buf, header_off + 2112)
    entry_num = _u32(buf, header_off + 2116)
    entry_size = _u32(buf, header_off + 2120)

    if page <= 0 or header_size <= 0:
        return []
    table_off = _align(header_size, page) + _align(vendor_ramdisk_size, page) + _align(dtb_size, page)
    if entry_size < 44 or entry_num == 0 or entry_size * entry_num > len(buf):
        return []

    entries: list[tuple[str, int, int]] = []
    for i in range(min(entry_num, 64)):
        off = table_off + i * entry_size
        if off + 44 > len(buf):
            break
        size = _u32(buf, off)
        offset = _u32(buf, off + 4)
        name = _cstr(buf, off + 12, 32)
        entries.append((name, offset, size))
    return entries


def parse_image(path: str | Path, with_hash: bool = True) -> BootImageInfo:
    """解析镜像并返回体检报告。任何异常都会被收敛到 error 字段，不抛出。"""
    p = Path(path)
    info = BootImageInfo(path=p, size=p.stat().st_size if p.exists() else 0)
    if not p.exists():
        info.error = "文件不存在"
        return info
    if info.size < 512:
        info.error = "文件太小，不是有效的引导镜像"
        return info

    try:
        with open(p, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                _fill_from_buffer(info, mm)
    except Exception as exc:  # noqa: BLE001
        info.error = f"读取镜像失败：{exc}"
        return info

    if with_hash:
        try:
            info.sha256 = sha256_file(p)
        except Exception:  # noqa: BLE001
            info.sha256 = ""
    return info


def _fill_from_buffer(info: BootImageInfo, buf) -> None:
    head = bytes(buf[:8])
    if head == BOOT_MAGIC:
        _parse_boot(info, buf)
    elif head == VENDOR_MAGIC:
        _parse_vendor_boot(info, buf)
    else:
        info.error = "不是 Android boot / init_boot / vendor_boot 镜像（缺少 ANDROID! / VNDRBOOT 魔数）"
        return

    # AVB 尾部签名
    if len(buf) >= 64:
        info.avb = bytes(buf[-64:-60]) == AVB_FOOTER_MAGIC

    # 是否已经打过 KernelSU 补丁
    _inspect_ramdisk(buf, info)

    if info.version in (0, 1, 2):
        info.warnings.append(
            f"该镜像是 boot header v{info.version}，KernelSU LKM 模式只支持 v3 及以上"
            "（Android 12 / 内核 5.10+）。此镜像无法用本工具修补。"
        )
    if info.kind == "boot" and info.ramdisk_size == 0:
        info.warnings.append(
            "该 boot 镜像不含 ramdisk。Android 13 及以上的设备请改选 init_boot.img 修补。"
        )
    if info.already_patched:
        info.warnings.append("ramdisk 中已存在 kernelsu.ko：这个镜像可能已经打过 KernelSU 补丁。")


def _parse_boot(info: BootImageInfo, buf) -> None:
    """boot / init_boot 头部解析。

    注意 v0~v2 与 v3~v4 是两套不同的结构：
      v0~v2: magic, kernel_size@8, kernel_addr, ramdisk_size@16, ramdisk_addr,
             second_size, second_addr, tags_addr, page_size@36, header_version@40
      v3~v4: magic, kernel_size@8, ramdisk_size@12, os_version@16,
             header_size@20, reserved[4], header_version@40, cmdline
    两者 header_version 恰好都在偏移 40，所以先读版本再分支。
    """
    version = _u32(buf, 40)

    if version <= 2:
        kernel_size = _u32(buf, 8)
        ramdisk_size = _u32(buf, 16)
        page_size = _u32(buf, 36)
        if not (512 <= page_size <= 65536):
            info.error = f"异常的页大小：{page_size}"
            return
        ramdisk_offset = page_size + _align(kernel_size, page_size)
        kind = "boot"
    else:
        # v3 / v4：页大小固定 4096，头部占一整页
        kernel_size = _u32(buf, 8)
        ramdisk_size = _u32(buf, 12)
        header_size = _u32(buf, 20)
        page_size = 4096
        ramdisk_offset = _align(header_size or page_size, page_size) + _align(kernel_size, page_size)
        kind = "init_boot" if (version >= 4 and kernel_size == 0 and ramdisk_size > 0) else "boot"

    info.version = version
    info.page_size = page_size
    info.kernel_size = kernel_size
    info.ramdisk_size = ramdisk_size
    info.ramdisk_offset = ramdisk_offset
    info.kind = kind

    if ramdisk_size and ramdisk_offset + ramdisk_size > len(buf):
        info.warnings.append("镜像长度与头部声明不一致（可能被截断），请确认文件完整。")

    if kernel_size and page_size + kernel_size <= len(buf):
        kernel = bytes(buf[page_size : page_size + kernel_size])
        info.kernel_version, info.kmi = _parse_kernel_meta(kernel)

    if ramdisk_size and ramdisk_offset < len(buf):
        ramdisk = bytes(buf[ramdisk_offset : min(ramdisk_offset + ramdisk_size, len(buf))])
        info.ramdisk_compression = detect_compression(ramdisk)


def _parse_vendor_boot(info: BootImageInfo, buf) -> None:
    version = _u32(buf, 8)
    page_size = _u32(buf, 12)
    vendor_ramdisk_size = _u32(buf, 24)
    header_size = _u32(buf, 2096)
    dtb_size = _u32(buf, 2100)

    info.kind = "vendor_boot"
    info.version = version
    info.page_size = page_size
    info.kernel_size = 0
    info.ramdisk_size = vendor_ramdisk_size
    info.ramdisk_offset = _align(header_size, page_size)

    blob = bytes(buf[info.ramdisk_offset : info.ramdisk_offset + vendor_ramdisk_size])
    info.ramdisk_compression = detect_compression(blob)

    # v4 有分片表，优先用 init_boot 分片判断压缩格式
    entries = _vendor_ramdisk_table(buf, 0, version)
    if entries:
        target = next((e for e in entries if e[0] == "init_boot"), None) or next(
            (e for e in entries if e[0] == ""), None
        )
        if target:
            name, off, size = target
            frag = blob[off : off + size] if size and off + size <= len(blob) else blob
            if frag:
                info.ramdisk_compression = detect_compression(frag)
        info.warnings.append(
            "vendor_boot 镜像：将修补其中的 init_boot ramdisk 分片（与官方 ksud 行为一致）。"
        )
    elif dtb_size:
        info.warnings.append("vendor_boot 镜像：未找到 ramdisk 分片表，将按整体 vendor_ramdisk 处理。")


def cpio_entries(data: bytes) -> list[tuple[str, int, int]]:
    """解析 newc 格式 cpio，返回 [(文件名, mode, 大小)]。"""
    entries: list[tuple[str, int, int]] = []
    pos = 0
    n = len(data)
    while pos + 110 <= n:
        if data[pos : pos + 6] not in (b"070701", b"070702"):
            break
        try:
            fields = [int(data[pos + 6 + i * 8 : pos + 6 + (i + 1) * 8], 16) for i in range(13)]
        except ValueError:
            break
        mode, filesize, namesize = fields[1], fields[6], fields[11]
        if namesize <= 0 or namesize > 4096:
            break
        name = data[pos + 110 : pos + 110 + namesize - 1].decode("utf-8", "replace")
        if name == "TRAILER!!!":
            break
        entries.append((name, mode, filesize))
        if len(entries) > 20000:
            break
        pos += 110 + namesize
        pos += (-pos) % 4
        pos += filesize
        pos += (-pos) % 4
    return entries


def cpio_read_files(data: bytes, wanted: tuple[str, ...]) -> list[tuple[str, bytes]]:
    """从 newc cpio 中取出指定文件的原始内容。"""
    out: list[tuple[str, bytes]] = []
    want = set(wanted)
    pos = 0
    n = len(data)
    while pos + 110 <= n:
        if data[pos : pos + 6] not in (b"070701", b"070702"):
            break
        try:
            fields = [int(data[pos + 6 + i * 8 : pos + 6 + (i + 1) * 8], 16) for i in range(13)]
        except ValueError:
            break
        filesize, namesize = fields[6], fields[11]
        if namesize <= 0 or namesize > 4096:
            break
        name = data[pos + 110 : pos + 110 + namesize - 1].decode("utf-8", "replace")
        if name == "TRAILER!!!":
            break
        body_start = pos + 110 + namesize
        body_start += (-body_start) % 4  # 文件名后面也要 4 字节对齐
        if name in want:
            out.append((name, data[body_start : body_start + filesize]))
        pos = body_start + filesize
        pos += (-pos) % 4
    return out


def _read_ramdisk(buf, info: "BootImageInfo") -> Optional[bytes]:
    if not info.ramdisk_size or info.ramdisk_offset >= len(buf):
        return None
    blob = bytes(buf[info.ramdisk_offset : info.ramdisk_offset + info.ramdisk_size])
    if not blob:
        return None
    data, _ = decompress(blob)
    return data


def _inspect_ramdisk(buf, info: "BootImageInfo") -> None:
    """解出 ramdisk 内容，判断是否已打补丁并记录条目名。"""
    data = _read_ramdisk(buf, info)
    if data is None:
        info.already_patched = None
        return
    info.ramdisk_entries = [name for name, _mode, _size in cpio_entries(data)]
    hit = b"kernelsu.ko" in data or b"init.real" in data or b"ksuinit" in data
    info.already_patched = bool(hit)

    # 取出 ksu_config 的实际内容，用于判断 allow_shell 等参数有没有真的写进去
    for name, body in cpio_read_files(data, ("ksu_config", "adb_debug.prop")):
        if name == "ksu_config":
            info.ksu_config = body.decode("utf-8", "replace").strip()
        elif name == "adb_debug.prop":
            info.has_force_debuggable = True
    if "force_debuggable" in info.ramdisk_entries:
        info.has_force_debuggable = True
