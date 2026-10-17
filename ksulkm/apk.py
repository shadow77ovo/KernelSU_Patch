"""KernelSU APK 解析：版本号、ABI、以及 APK 内置的 KMI 列表。

为什么需要读 APK？
  1. 版本号决定了要下载哪一份官方 PC 端 ksud 引擎（版本必须一一对应，
     否则内置的 kernelsu.ko / ksuinit 与 App 版本不匹配）。
  2. APK 里的 libksud.so 就是打包进 App 的 ksud 本体，它把
     `<kmi>_kernelsu.ko` 与 `ksuinit` 以明文形式内嵌在 ELF 里，
     因此可以直接从 APK 读出“这版支持哪些 KMI”，用于界面下拉框与校验。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .util import human_size, sha256_file

# rust_embed 把资源名以字符串形式写进 .rodata，这里直接把它们捞出来
KMI_IN_SO_RE = re.compile(rb"((?:android\d{1,2})-(?:\d+\.\d+))_kernelsu\.ko")
VERSION_IN_NAME_RE = re.compile(r"[vV]?(\d+\.\d+(?:\.\d+)?)")

# Android 二进制 XML 的块类型
RES_XML_TYPE = 0x0003
RES_STRING_POOL_TYPE = 0x0001
RES_XML_START_ELEMENT_TYPE = 0x0102

TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11


def _natural_key(kmi: str):
    m = re.match(r"android(\d+)-(\d+)\.(\d+)", kmi)
    if not m:
        return (999, 999, 999)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


class _StringPool:
    """Android ResStringPool 的最小实现（只读）。"""

    def __init__(self, data: bytes, offset: int):
        self.data = data
        self.offset = offset
        self.strings: list[str] = []
        self._load()

    def _load(self) -> None:
        d, o = self.data, self.offset
        if len(d) < o + 28:
            return
        header_size = int.from_bytes(d[o + 2 : o + 4], "little")
        string_count = int.from_bytes(d[o + 8 : o + 12], "little")
        flags = int.from_bytes(d[o + 16 : o + 20], "little")
        strings_start = int.from_bytes(d[o + 20 : o + 24], "little")
        utf8 = bool(flags & (1 << 8))

        offsets_at = o + header_size
        base = o + strings_start
        for i in range(min(string_count, 20000)):
            try:
                p = int.from_bytes(d[offsets_at + i * 4 : offsets_at + i * 4 + 4], "little")
                pos = base + p
                if pos >= len(d):
                    break
                if utf8:
                    # 变长 UTF-8 长度（1~2 字节）
                    n = d[pos]
                    pos += 1
                    if n & 0x80:
                        n = ((n & 0x7F) << 8) | d[pos]
                        pos += 1
                    raw = d[pos : pos + n]
                    self.strings.append(raw.decode("utf-8", "replace"))
                else:
                    n = int.from_bytes(d[pos : pos + 2], "little")
                    pos += 2
                    if n & 0x8000:
                        n = ((n & 0x7FFF) << 16) | int.from_bytes(d[pos : pos + 2], "little")
                        pos += 2
                    raw = d[pos : pos + n * 2]
                    self.strings.append(raw.decode("utf-16-le", "replace"))
            except Exception:  # noqa: BLE001
                break

    def get(self, index: int) -> str:
        if 0 <= index < len(self.strings):
            return self.strings[index]
        return ""


def parse_binary_manifest(data: bytes) -> dict[str, str]:
    """从 AndroidManifest.xml（二进制 XML）里取 versionName / versionCode / package。"""
    result: dict[str, str] = {}
    if len(data) < 8 or int.from_bytes(data[0:2], "little") != RES_XML_TYPE:
        return result

    pool: Optional[_StringPool] = None
    pos = int.from_bytes(data[2:4], "little") or 8

    while pos + 8 <= len(data):
        ctype = int.from_bytes(data[pos : pos + 2], "little")
        header_size = int.from_bytes(data[pos + 2 : pos + 4], "little")
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if size <= 0 or pos + size > len(data):
            break

        if ctype == RES_STRING_POOL_TYPE and pool is None:
            pool = _StringPool(data, pos)
        elif ctype == RES_XML_START_ELEMENT_TYPE and pool is not None:
            if _read_element(data, pos, header_size, pool, result):
                break
        pos += size
    return result


def _read_element(data: bytes, chunk: int, header_size: int, pool: _StringPool, out: dict) -> bool:
    """解析一个 START_ELEMENT，命中 manifest 根节点则返回 True。"""
    attr_start = int.from_bytes(data[chunk + 24 : chunk + 26], "little")
    attr_size = int.from_bytes(data[chunk + 26 : chunk + 28], "little")
    attr_count = int.from_bytes(data[chunk + 28 : chunk + 30], "little")
    if attr_size < 20:
        attr_size = 20

    found_manifest = False
    for i in range(min(attr_count, 64)):
        off = chunk + 16 + attr_start + i * attr_size
        if off + 20 > len(data):
            break
        name_idx = int.from_bytes(data[off + 4 : off + 8], "little")
        raw_value = int.from_bytes(data[off + 8 : off + 12], "little")
        data_type = data[off + 15]
        value = int.from_bytes(data[off + 16 : off + 20], "little")

        name = pool.get(name_idx)
        if name == "versionName":
            out["versionName"] = pool.get(value) if data_type == TYPE_STRING else pool.get(raw_value)
            found_manifest = True
        elif name == "versionCode":
            out["versionCode"] = str(value) if data_type in (TYPE_INT_DEC, TYPE_INT_HEX) else pool.get(value)
            found_manifest = True
        elif name == "package":
            out["package"] = pool.get(value) if data_type == TYPE_STRING else pool.get(raw_value)
            found_manifest = True

    return found_manifest and "versionName" in out


@dataclass
class ApkInfo:
    path: Path
    size: int = 0
    version_name: str = ""
    version_code: str = ""
    package: str = ""
    abis: list[str] = field(default_factory=list)
    kmis: list[str] = field(default_factory=list)
    has_ksuinit: bool = False
    version_source: str = ""
    sha256: str = ""
    error: str = ""

    @property
    def release_tag(self) -> str:
        """KernelSU 的 release tag 形如 v3.3.1。"""
        v = self.version_name.strip()
        if not v:
            return ""
        return v if v.startswith("v") else f"v{v}"

    @property
    def looks_like_release_version(self) -> bool:
        """dev 构建的版本号会带 -g<hash> 之类后缀，不一定有对应 release。"""
        return bool(re.fullmatch(r"\d+\.\d+(?:\.\d+)?", self.version_name.strip().lstrip("v")))

    def summary_lines(self) -> list[str]:
        lines = [
            f"版本：{self.version_name or '未知'}"
            + (f"（versionCode {self.version_code}）" if self.version_code else ""),
            f"大小：{human_size(self.size)}    ABI：{', '.join(self.abis) or '未知'}",
        ]
        if self.kmis:
            lines.append(f"内置 KMI（{len(self.kmis)} 个）：{', '.join(self.kmis)}")
        else:
            lines.append("内置 KMI：未从 APK 中读到（不影响修补，由 ksud 引擎自带资源决定）")
        return lines


def parse_apk(path: str | Path, with_hash: bool = False) -> ApkInfo:
    p = Path(path)
    info = ApkInfo(path=p)
    if not p.exists():
        info.error = "APK 文件不存在"
        return info
    info.size = p.stat().st_size

    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()

            info.abis = sorted(
                {n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2}
            )

            # 1) 优先读 Gradle 写入的元数据（最省事也最准）
            for meta in (
                "META-INF/com/android/build/gradle/app-metadata.properties",
                "META-INF/com/android/build/gradle/apk-metadata.properties",
            ):
                if meta in names:
                    text = zf.read(meta).decode("utf-8", "replace")
                    for line in text.splitlines():
                        key, _, val = line.partition("=")
                        key, val = key.strip(), val.strip()
                        if key == "versionName" and not info.version_name:
                            info.version_name, info.version_source = val, "app-metadata"
                        elif key == "versionCode" and not info.version_code:
                            info.version_code = val
                    if info.version_name:
                        break

            # 2) 再解析 AndroidManifest.xml（二进制 XML）
            if not info.version_name and "AndroidManifest.xml" in names:
                try:
                    manifest = parse_binary_manifest(zf.read("AndroidManifest.xml"))
                    info.version_name = manifest.get("versionName", "")
                    info.version_code = info.version_code or manifest.get("versionCode", "")
                    info.package = manifest.get("package", "")
                    if info.version_name:
                        info.version_source = "AndroidManifest"
                except Exception:  # noqa: BLE001
                    pass

            # 3) 兜底：从文件名里猜，例如 KernelSU_v3.3.1_32680-release.apk
            if not info.version_name:
                m = VERSION_IN_NAME_RE.search(p.name)
                if m:
                    info.version_name, info.version_source = m.group(1), "文件名"

            # 4) 扫描 libksud.so，读出内嵌的 KMI 资源名
            for so in [n for n in names if n.startswith("lib/") and n.endswith("libksud.so")]:
                blob = zf.read(so)
                for kmi in KMI_IN_SO_RE.findall(blob):
                    name = kmi.decode()
                    if name not in info.kmis:
                        info.kmis.append(name)
                if b"ksuinit" in blob:
                    info.has_ksuinit = True
                if info.kmis:
                    break

            # 5) 少数打包方式会直接放 .ko 文件
            for n in names:
                if n.endswith("_kernelsu.ko"):
                    kmi = n.rsplit("/", 1)[-1][: -len("_kernelsu.ko")]
                    if kmi not in info.kmis:
                        info.kmis.append(kmi)

            info.kmis.sort(key=_natural_key)
    except zipfile.BadZipFile:
        info.error = "不是有效的 APK（zip 解析失败）"
        return info
    except Exception as exc:  # noqa: BLE001
        info.error = f"解析 APK 失败：{exc}"
        return info

    if with_hash:
        try:
            info.sha256 = sha256_file(p)
        except Exception:  # noqa: BLE001
            info.sha256 = ""
    return info
