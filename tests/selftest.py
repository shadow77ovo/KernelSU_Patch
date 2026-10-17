"""自检脚本：用合成数据验证解析与编排逻辑，不需要真机、不需要联网。

运行：python tests/selftest.py
"""

from __future__ import annotations

import gzip
import io
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ksulkm import apk as apk_mod
from ksulkm import bootimg as boot_mod
from ksulkm import engine as engine_mod
from ksulkm import patcher as patcher_mod
from ksulkm.apk import parse_binary_manifest

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------- 构造工具
def cpio_newc(files: dict[str, bytes], modes: dict[str, int] | None = None) -> bytes:
    modes = modes or {}
    out = io.BytesIO()
    ino = 1
    for name, data in files.items():
        mode = modes.get(name, 0o100644)
        hdr = b"070701"
        hdr += b"%08X" % ino
        hdr += b"%08X" % mode
        hdr += b"%08X" % 0
        hdr += b"%08X" % 0
        hdr += b"%08X" % 1
        hdr += b"%08X" % 0
        hdr += b"%08X" % len(data)
        hdr += b"%08X" % 0
        hdr += b"%08X" % 0
        hdr += b"%08X" % 0
        hdr += b"%08X" % 0
        hdr += b"%08X" % (len(name) + 1)
        hdr += b"%08X" % 0
        out.write(hdr)
        out.write(name.encode() + b"\x00")
        pad = (-(len(hdr) + len(name) + 1)) % 4
        out.write(b"\x00" * pad)
        out.write(data)
        out.write(b"\x00" * ((-len(data)) % 4))
        ino += 1

    name = "TRAILER!!!"
    hdr = b"070701" + b"0" * 8 * 13
    hdr = b"070701"
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % 1
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % 0
    hdr += b"%08X" % (len(name) + 1)
    hdr += b"%08X" % 0
    out.write(hdr)
    out.write(name.encode() + b"\x00")
    out.write(b"\x00" * ((-(len(hdr) + len(name) + 1)) % 4))
    return out.getvalue()


def align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


def make_boot_v4(ramdisk: bytes, kernel: bytes = b"", version: int = 4) -> bytes:
    page = 4096
    header_size = 1584 if version == 4 else 1580
    hdr = bytearray(header_size)
    hdr[0:8] = b"ANDROID!"
    struct.pack_into("<I", hdr, 8, len(kernel))
    struct.pack_into("<I", hdr, 12, len(ramdisk))
    struct.pack_into("<I", hdr, 16, 0x0C000000)  # os_version
    struct.pack_into("<I", hdr, 20, header_size)
    struct.pack_into("<I", hdr, 40, version)
    buf = bytes(hdr) + b"\x00" * (page - header_size)
    buf += kernel + b"\x00" * (align(len(kernel), page) - len(kernel))
    buf += ramdisk + b"\x00" * (align(len(ramdisk), page) - len(ramdisk))
    return buf


def make_boot_v0(kernel: bytes, ramdisk: bytes) -> bytes:
    page = 2048
    hdr = bytearray(1632)
    hdr[0:8] = b"ANDROID!"
    struct.pack_into("<I", hdr, 8, len(kernel))
    struct.pack_into("<I", hdr, 16, len(ramdisk))
    struct.pack_into("<I", hdr, 36, page)
    struct.pack_into("<I", hdr, 40, 0)
    buf = bytes(hdr) + b"\x00" * (page - 1632)
    buf += kernel + b"\x00" * (align(len(kernel), page) - len(kernel))
    buf += ramdisk + b"\x00" * (align(len(ramdisk), page) - len(ramdisk))
    return buf


def make_vendor_boot_v4(ramdisk: bytes) -> bytes:
    page = 4096
    header_size = 2128
    hdr = bytearray(header_size)
    hdr[0:8] = b"VNDRBOOT"
    struct.pack_into("<I", hdr, 8, 4)
    struct.pack_into("<I", hdr, 12, page)
    struct.pack_into("<I", hdr, 24, len(ramdisk))
    struct.pack_into("<I", hdr, 2096, header_size)
    struct.pack_into("<I", hdr, 2100, 0)  # dtb_size

    table = bytearray(108)
    struct.pack_into("<I", table, 0, len(ramdisk))
    struct.pack_into("<I", table, 4, 0)
    struct.pack_into("<I", table, 8, 2)
    table[12 : 12 + 9] = b"init_boot"
    struct.pack_into("<I", hdr, 2112, 108)
    struct.pack_into("<I", hdr, 2116, 1)
    struct.pack_into("<I", hdr, 2120, 108)
    struct.pack_into("<I", hdr, 2124, 0)

    buf = bytes(hdr) + b"\x00" * (page - header_size)
    buf += ramdisk + b"\x00" * (align(len(ramdisk), page) - len(ramdisk))
    buf += bytes(table) + b"\x00" * (page - len(table))
    return buf


def build_axml(attrs: dict[str, str], root_name: str = "manifest") -> bytes:
    """构造一个最小的二进制 AndroidManifest.xml（UTF-8 字符串池）。"""
    strings = list(attrs.keys()) + list(attrs.values()) + [root_name]
    index = {s: i for i, s in enumerate(strings)}

    data = bytearray()
    offsets = []
    for s in strings:
        offsets.append(len(data))
        raw = s.encode("utf-8")
        if len(raw) > 0x7F:
            data += bytes([0x80 | (len(raw) >> 8), len(raw) & 0xFF])
        else:
            data.append(len(raw))
        data += raw + b"\x00"
    data += b"\x00" * ((-len(data)) % 4)

    pool_header_size = 28
    pool_size = pool_header_size + 4 * len(strings) + len(data)
    pool = bytearray()
    pool += struct.pack("<HHI", 0x0001, pool_header_size, pool_size)
    pool += struct.pack("<IIIII", len(strings), 0, 0x100, pool_header_size + 4 * len(strings), 0)
    for off in offsets:
        pool += struct.pack("<I", off)
    pool += data

    n = len(attrs)
    elem_size = 16 + 20 + 20 * n
    elem = bytearray()
    elem += struct.pack("<HHI", 0x0102, 16, elem_size)
    elem += struct.pack("<II", 1, 0xFFFFFFFF)  # lineNumber, comment
    elem += struct.pack("<II", 0xFFFFFFFF, index[root_name])  # ns, name
    elem += struct.pack("<HHHHHH", 20, 20, n, 0, 0, 0)
    for k, v in attrs.items():
        elem += struct.pack("<III", 0xFFFFFFFF, index[k], index[v])
        elem += struct.pack("<HBBI", 8, 0, 0x03, index[v])

    total = 8 + len(pool) + len(elem)
    head = struct.pack("<HHI", 0x0003, 8, total)
    return head + bytes(pool) + bytes(elem)


def make_apk(path: Path, version: str = "3.3.1", with_metadata: bool = True) -> None:
    so = (
        b"\x7fELF\x02\x01\x01\x00"
        + b"\x00" * 32
        + b"android13-5.15_kernelsu.ko\x00"
        + b"android13-5.10_kernelsu.ko\x00"
        + b"android12-5.10_kernelsu.ko\x00"
        + b"ksuinit\x00"
        + b"\x00" * 64
    )
    manifest = build_axml(
        {"versionName": version, "versionCode": "32680", "package": "me.weishu.kernelsu"}
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", manifest)
        zf.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 32)
        zf.writestr("lib/arm64-v8a/libksud.so", so)
        zf.writestr("lib/x86_64/libksud.so", so)
        if with_metadata:
            zf.writestr(
                "META-INF/com/android/build/gradle/app-metadata.properties",
                f"versionCode=32680\nversionName={version}\n",
            )


# ---------------------------------------------------------------- 测试用例
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ksu-selftest-"))
    print(f"临时目录：{tmp}\n")

    cpio = cpio_newc(
        {"init": b"#!/system/bin/sh\n", "system/bin/foo": b"bar"},
        {"init": 0o100755, "system/bin/foo": 0o100755},
    )
    ramdisk_gz = gzip.compress(cpio)

    print("[1] boot v4 / init_boot 解析")
    img = tmp / "init_boot.img"
    img.write_bytes(make_boot_v4(ramdisk_gz))
    info = boot_mod.parse_image(img, with_hash=False)
    check("无错误", not info.error, info.error)
    check("识别为 init_boot", info.kind == "init_boot", info.kind)
    check("版本为 4", info.version == 4, str(info.version))
    check("ramdisk 大小正确", info.ramdisk_size == len(ramdisk_gz))
    check("压缩格式 gzip", info.ramdisk_compression == "gzip", info.ramdisk_compression)
    check("未被 patch", info.already_patched is False)
    check("引擎可用", info.supported_by_engine)
    check("无内核时不猜 KMI", info.kmi == "")

    print("[2] boot v3 + 内核版本 → KMI 推断")
    kernel = (
        b"\x00" * 256
        + b"Linux version 5.15.94-android13-8-g1a2b3c4d (builder@host) #1 SMP PREEMPT"
        + b"\x00" * 64
    )
    img3 = tmp / "boot.img"
    img3.write_bytes(make_boot_v4(ramdisk_gz, kernel=kernel, version=3))
    info3 = boot_mod.parse_image(img3, with_hash=False)
    check("识别为 boot", info3.kind == "boot", info3.kind)
    check("版本为 3", info3.version == 3)
    check("KMI 推断为 android13-5.15", info3.kmi == "android13-5.15", info3.kmi)
    check("内核版本串", info3.kernel_version.startswith("5.15.94"), info3.kernel_version)

    print("[3] 内核为压缩（gzip）时也能推断")
    kernel_gz = gzip.compress(kernel)
    img3b = tmp / "boot_gz.img"
    img3b.write_bytes(make_boot_v4(ramdisk_gz, kernel=kernel_gz, version=3))
    info3b = boot_mod.parse_image(img3b, with_hash=False)
    check("压缩内核 KMI 正确", info3b.kmi == "android13-5.15", info3b.kmi)

    print("[4] boot v0 应被判定为不支持")
    img0 = tmp / "old_boot.img"
    img0.write_bytes(make_boot_v0(kernel, ramdisk_gz))
    info0 = boot_mod.parse_image(img0, with_hash=False)
    check("版本为 0", info0.version == 0, str(info0.version))
    check("页大小 2048", info0.page_size == 2048, str(info0.page_size))
    check("不支持", not info0.supported_by_engine)
    check("给出提示", any("v3" in w for w in info0.warnings))

    print("[5] vendor_boot v4 解析")
    vb = tmp / "vendor_boot.img"
    vb.write_bytes(make_vendor_boot_v4(ramdisk_gz))
    infov = boot_mod.parse_image(vb, with_hash=False)
    check("识别为 vendor_boot", infov.kind == "vendor_boot", infov.kind)
    check("版本 4", infov.version == 4)
    check("ramdisk 压缩 gzip", infov.ramdisk_compression == "gzip", infov.ramdisk_compression)
    check("支持", infov.supported_by_engine)

    print("[6] 已打补丁的镜像应被识别")
    patched_cpio = cpio_newc(
        {
            "init": b"ksuinit",
            "kernelsu.ko": b"\x7fELF" * 10,
            "init.real": b"x",
            "ksu_config": b"allow_shell=1 bundled=1",
        }
    )
    imgp = tmp / "patched.img"
    imgp.write_bytes(make_boot_v4(gzip.compress(patched_cpio)))
    infop = boot_mod.parse_image(imgp, with_hash=False)
    check("检测到 kernelsu.ko", infop.already_patched is True)
    check("读到 ksu_config", infop.ksu_config == "allow_shell=1 bundled=1", repr(infop.ksu_config))
    check("能判定 allow_shell", "allow_shell=1" in infop.ksu_config)
    check("给出已打补丁提示", any("已经打过" in w for w in infop.warnings))

    print("[7] 非镜像文件")
    junk = tmp / "junk.img"
    junk.write_bytes(b"NOT-AN-IMAGE" + b"\x00" * 2000)
    infoj = boot_mod.parse_image(junk, with_hash=False)
    check("报错", bool(infoj.error))
    check("不支持", not infoj.supported_by_engine)

    print("[8] 二进制 AndroidManifest.xml 解析")
    axml = build_axml({"versionName": "3.3.1", "versionCode": "32680", "package": "me.weishu.kernelsu"})
    parsed = parse_binary_manifest(axml)
    check("读到 versionName", parsed.get("versionName") == "3.3.1", str(parsed))
    check("读到 versionCode", parsed.get("versionCode") == "32680", str(parsed))
    check("读到 package", parsed.get("package") == "me.weishu.kernelsu", str(parsed))

    print("[9] APK 解析（元数据 / Manifest / 文件名 三条路径）")
    apk1 = tmp / "KernelSU_v3.3.1_32680-release.apk"
    make_apk(apk1, with_metadata=True)
    a1 = apk_mod.parse_apk(apk1)
    check("版本 3.3.1（app-metadata）", a1.version_name == "3.3.1", a1.version_name)
    check("来源正确", a1.version_source == "app-metadata", a1.version_source)
    check("读到 3 个 KMI", len(a1.kmis) == 3, str(a1.kmis))
    check("KMI 排序", a1.kmis == ["android12-5.10", "android13-5.10", "android13-5.15"], str(a1.kmis))
    check("识别 ABI", "arm64-v8a" in a1.abis, str(a1.abis))
    check("识别 ksuinit", a1.has_ksuinit)
    check("release tag", a1.release_tag == "v3.3.1", a1.release_tag)

    apk2 = tmp / "no_meta.apk"
    make_apk(apk2, version="3.2.0", with_metadata=False)
    a2 = apk_mod.parse_apk(apk2)
    check("无元数据时走 Manifest", a2.version_name == "3.2.0", a2.version_name)
    check("来源为 Manifest", a2.version_source == "AndroidManifest", a2.version_source)

    bad = tmp / "bad.apk"
    bad.write_bytes(b"not a zip")
    a3 = apk_mod.parse_apk(bad)
    check("坏 APK 报错", bool(a3.error))

    print("[10] 命令拼装")
    cmd = engine_mod.build_command(
        ksud="C:/tools/ksud.exe",
        image=img,
        out_dir=tmp,
        out_name="out.img",
        kmi="android13-5.15",
        arch="arm64",
        allow_shell=True,
    )
    check("包含 boot-patch", cmd[1] == "boot-patch")
    check("-b 传镜像", "-b" in cmd and cmd[cmd.index("-b") + 1].endswith("init_boot.img"))
    check("--kmi 正确", cmd[cmd.index("--kmi") + 1] == "android13-5.15")
    check("--arch 归一化", cmd[cmd.index("--arch") + 1] == "aarch64")
    check("--allow-shell", "--allow-shell" in cmd)
    check("不传 Android 专属参数", "--backup" not in cmd and "--flash" not in cmd and "-u" not in cmd)

    cmd2 = engine_mod.build_command(
        ksud="ksud.exe", image=img, out_dir=tmp, out_name="o.img", module="kernelsu.ko"
    )
    check("自定义 .ko 用 -m", "-m" in cmd2 and "--kmi" not in cmd2)

    print("[11] 编排流程（模拟引擎）")
    out_file = tmp / "init_boot-ksu-patched.img"
    fake_log = (
        "- Preparing assets\n"
        "- Unpacking boot image\n"
        "- Adding KernelSU LKM\n"
        "- Repacking boot image\n"
        "- Output file is written to\n"
        f"- {out_file}\n"
        "- Done!"
    )

    origin_probe = engine_mod.probe_engine
    origin_run = engine_mod.run_stream
    try:
        engine_mod.probe_engine = lambda p: engine_mod.EngineInfo(
            path=Path(p), version="ksud 3.3.1", supports_boot_patch=True, ok=True, message="ok"
        )

        def fake_run(cmd, on_line, cwd=None):
            # 模拟引擎产物：把源镜像的 ramdisk 换成含 kernelsu.ko 的版本
            out_file.write_bytes(make_boot_v4(gzip.compress(patched_cpio)))
            for line in fake_log.splitlines():
                on_line(line)
            return 0, fake_log

        engine_mod.run_stream = fake_run
        patcher_mod.run_stream = fake_run
        patcher_mod.probe_engine = engine_mod.probe_engine

        logs: list[str] = []
        result = patcher_mod.patch(
            patcher_mod.PatchRequest(
                image=str(img), apk=str(apk1), ksud=str(apk1), kmi="android13-5.15"
            ),
            on_log=logs.append,
        )
        check("修补成功", result.ok, result.error)
        check("产物路径正确", Path(result.output) == out_file, result.output)
        check("产物校验通过", result.verified)
        check("日志完整", any("Repacking" in line for line in logs))
        check("记录了命令", result.command and result.command[1] == "boot-patch")

        # 缺少 KMI 且无 .ko → 必须明确报错
        res2 = patcher_mod.patch(
            patcher_mod.PatchRequest(image=str(img), apk=str(apk1), ksud=str(apk1), kmi=""),
        )
        # init_boot 无内核，自动识别失败，但 APK 列出了 KMI → 提示手动选择
        check("缺 KMI 时给出可选项", (not res2.ok) and "手动选择" in res2.error, res2.error)

        # 指定 KMI 后应成功
        res3 = patcher_mod.patch(
            patcher_mod.PatchRequest(image=str(img), apk=str(apk1), ksud=str(apk1), kmi="android13-5.15"),
        )
        check("手动 KMI 后成功", res3.ok, res3.error)

        # 不支持的镜像应被拦下
        res4 = patcher_mod.patch(
            patcher_mod.PatchRequest(image=str(img0), apk=str(apk1), ksud=str(apk1), kmi="android12-5.10"),
        )
        check("v0 镜像被拦下", (not res4.ok) and "v3" in res4.error, res4.error)
    finally:
        engine_mod.probe_engine = origin_probe
        engine_mod.run_stream = origin_run
        patcher_mod.run_stream = origin_run
        patcher_mod.probe_engine = origin_probe

    print("[12] LZ4 解码（纯 Python 实现）")
    from ksulkm import lz4

    # 手写一个合法的 LZ4 block："abcd" + (offset=4, len=8) -> "abcdabcdabcd"
    block = b"\x44" + b"abcd" + b"\x04\x00"
    legacy = b"\x02\x21\x4c\x18" + struct.pack("<I", len(block)) + block
    check("legacy 格式解压", lz4.decompress(legacy) == b"abcdabcdabcd", str(lz4.decompress(legacy)))
    frame = b"\x04\x22\x4d\x18" + b"\x60\x40\x00" + struct.pack("<I", len(block)) + block + b"\x00\x00\x00\x00"
    check("frame 格式解压", lz4.decompress(frame) == b"abcdabcdabcd", str(lz4.decompress(frame)))
    check("非 lz4 数据返回 None", lz4.decompress(b"\x1f\x8b\x08\x00") is None)

    print("[13] cpio 条目解析")
    ents = boot_mod.cpio_entries(cpio)
    names = [e[0] for e in ents]
    check("解析出全部条目", names == ["init", "system/bin/foo"], str(names))
    check("mode 正确", ents[0][1] == 0o100755, oct(ents[0][1]))
    check("大小正确", ents[0][2] == len(b"#!/system/bin/sh\n"), str(ents[0][2]))

    print("[14] payload.exe 参数与输出解析")
    from ksulkm import payload_tool

    args = payload_tool.build_args(Path("payload.exe"), "https://example.com/ota.zip", ["boot"], "out")
    check("URL 走 --url", args[:2] == ["payload.exe", "--url"], str(args))
    check("带分区与输出目录", "boot" in args and args[-2] == "--out", str(args))
    args2 = payload_tool.build_args(Path("payload.exe"), r"D://ota//payload.bin", list_only=True)
    check("本地走路径 + --list", args2[-1] == "--list" and "payload.bin" in args2[1], str(args2))
    pm = payload_tool.PARTITION_RE.match("init_boot             8.0 MB")
    check("分区行解析", bool(pm) and pm.group(1) == "init_boot" and pm.group(2) == "8.0")
    gm = payload_tool.PROGRESS_RE.match("boot.img         [####------]  50.6%")
    check("进度行解析", bool(gm) and gm.group(1) == "boot.img" and gm.group(2) == "50.6")

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
