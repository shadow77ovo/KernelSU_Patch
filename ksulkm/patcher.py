"""修补流程编排：预检 → 选 KMI → 取引擎 → boot-patch → 产物校验。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import apk as apk_mod
from . import bootimg as boot_mod
from .downloader import DownloadError, engine_cache_path, ensure_engine
from .engine import build_command, parse_output_path, probe_engine, run_stream
from .util import default_out_name, ensure_dir, human_size, unique_path

LogFn = Callable[[str], None]
StageFn = Callable[[str], None]


@dataclass
class PatchRequest:
    image: str
    apk: str
    out_dir: str = ""
    out_name: str = ""
    kmi: str = ""
    module: str = ""
    arch: str = "aarch64"
    allow_shell: bool = False
    enable_adbd: bool = False
    block_modules: str = ""
    ksud: str = ""
    dry_run: bool = False


@dataclass
class PatchResult:
    ok: bool = False
    output: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    log: str = ""
    command: list[str] = field(default_factory=list)
    image_info: Optional[boot_mod.BootImageInfo] = None
    apk_info: Optional[apk_mod.ApkInfo] = None
    kmi: str = ""
    out_size: int = 0
    verified: bool = False


class _Ctx:
    """把日志/阶段回调收敛到一起，顺便累积完整日志。"""

    def __init__(self, on_log: Optional[LogFn], on_stage: Optional[StageFn]):
        self._on_log = on_log or (lambda _m: None)
        self._on_stage = on_stage or (lambda _m: None)
        self.lines: list[str] = []

    def log(self, msg: str = "") -> None:
        self.lines.append(msg)
        self._on_log(msg)

    def stage(self, msg: str) -> None:
        self._on_stage(msg)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _fail(ctx: _Ctx, result: PatchResult, msg: str) -> PatchResult:
    result.ok = False
    result.error = msg
    ctx.log(f"[错误] {msg}")
    result.log = ctx.text
    return result


def preflight(image: str, apk_path: str) -> tuple[boot_mod.BootImageInfo, apk_mod.ApkInfo]:
    """只做检查，不执行任何修补，供界面实时展示。"""
    return boot_mod.parse_image(image), apk_mod.parse_apk(apk_path)


def patch(
    req: PatchRequest,
    on_log: Optional[LogFn] = None,
    on_stage: Optional[StageFn] = None,
) -> PatchResult:
    ctx = _Ctx(on_log, on_stage)
    result = PatchResult()

    # ---------- 1. 预检镜像 ----------
    ctx.stage("检查镜像")
    image_path = Path(req.image).expanduser()
    if not image_path.is_file():
        return _fail(ctx, result, f"镜像文件不存在：{image_path}")

    info = boot_mod.parse_image(image_path)
    result.image_info = info
    if info.error:
        return _fail(ctx, result, info.error)

    ctx.log(f"镜像：{image_path.name}（{human_size(info.size)}）")
    for line in info.summary_lines():
        ctx.log(f"  {line}")
    result.warnings.extend(info.warnings)
    for w in info.warnings:
        ctx.log(f"  [提示] {w}")

    if not info.supported_by_engine:
        return _fail(
            ctx,
            result,
            f"该镜像为 {info.kind_label} {info.version_label}，KernelSU LKM 模式要求 boot header v3 及以上。",
        )
    if info.kind == "boot" and info.ramdisk_size == 0:
        ctx.log("  [提示] 该 boot 镜像没有 ramdisk，若设备是 Android 13+ 请改选 init_boot.img。")

    # ---------- 2. 解析 APK ----------
    ctx.stage("读取 KernelSU APK")
    apk_path = Path(req.apk).expanduser()
    if not apk_path.is_file():
        return _fail(ctx, result, f"APK 文件不存在：{apk_path}")

    apk_info = apk_mod.parse_apk(apk_path)
    result.apk_info = apk_info
    if apk_info.error:
        return _fail(ctx, result, apk_info.error)
    if not apk_info.version_name:
        return _fail(ctx, result, "无法从 APK 中读出 KernelSU 版本号，请换一个官方 release 版 APK。")

    ctx.log(f"KernelSU：{apk_info.version_name}（{human_size(apk_info.size)}）")
    for line in apk_info.summary_lines():
        ctx.log(f"  {line}")

    # ---------- 3. 决定 KMI ----------
    ctx.stage("确定 KMI")
    kmi = (req.kmi or "").strip()
    if kmi.lower() in ("auto", "自动检测"):
        kmi = ""
    if not kmi:
        kmi = info.kmi
    if not kmi and req.module:
        kmi = ""  # 自带 .ko 时不需要 KMI
    if not kmi and not req.module:
        hint = "、".join(apk_info.kmis) if apk_info.kmis else "（APK 未列出）"
        return _fail(
            ctx,
            result,
            "无法从镜像自动识别 KMI（init_boot 不含内核信息），请手动选择。可选：" + hint,
        )
    result.kmi = kmi
    if kmi:
        ctx.log(f"  KMI：{kmi}（{'手动指定' if req.kmi and req.kmi != '自动检测' else '由内核版本推断'}）")
        if info.kmi and kmi != info.kmi:
            msg = (
                f"手动指定的 KMI（{kmi}）与镜像内核推断出的（{info.kmi}）不一致，"
                "刷入后 kernelsu.ko 很可能加载失败。"
            )
            result.warnings.append(msg)
            ctx.log(f"  [警告] {msg}")
        if apk_info.kmis and kmi not in apk_info.kmis:
            msg = f"KMI {kmi} 不在该 APK 内置列表中（{', '.join(apk_info.kmis)}），补丁可能无法加载。"
            result.warnings.append(msg)
            ctx.log(f"  [警告] {msg}")

    if req.module:
        mod = Path(req.module).expanduser()
        if not mod.is_file():
            return _fail(ctx, result, f"自定义模块不存在：{mod}")
        ctx.log(f"  使用自定义模块：{mod.name}（{human_size(mod.stat().st_size)}）")

    # ---------- 4. 输出路径 ----------
    out_dir = Path(req.out_dir).expanduser() if req.out_dir else image_path.parent
    ensure_dir(out_dir)
    out_name = req.out_name.strip() or default_out_name(image_path)
    if not out_name.lower().endswith(".img"):
        out_name += ".img"
    target = unique_path(out_dir / out_name)
    if target.resolve() == image_path.resolve():
        return _fail(ctx, result, "输出文件与源镜像相同，已中止（不会覆盖源镜像）。")
    ctx.log(f"输出：{target}")

    # ---------- 5. 准备引擎 ----------
    ctx.stage("准备 ksud 引擎")
    if req.ksud:
        ksud = Path(req.ksud).expanduser()
    elif req.dry_run:
        ksud = engine_cache_path(apk_info.release_tag)
        ctx.log(f"  [dry-run] 跳过下载与检测，假定引擎路径：{ksud}")
    else:
        try:
            ksud = ensure_engine(apk_info.version_name, log=ctx.log)
        except DownloadError as exc:
            return _fail(ctx, result, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(ctx, result, f"获取 ksud 引擎失败：{exc}")

    if not req.dry_run:
        engine_info = probe_engine(ksud)
        if not engine_info.ok:
            return _fail(ctx, result, f"ksud 引擎不可用：{engine_info.message}（{ksud}）")
        ctx.log(f"引擎：{ksud}")
        if engine_info.version:
            ctx.log(f"  {engine_info.version}")

    # ---------- 6. 执行 ----------
    cmd = build_command(
        ksud=ksud,
        image=image_path,
        out_dir=target.parent,
        out_name=target.name,
        kmi=kmi,
        module=req.module,
        arch=req.arch,
        allow_shell=req.allow_shell,
        enable_adbd=req.enable_adbd,
        block_modules=req.block_modules,
    )
    result.command = cmd
    ctx.log("")
    ctx.log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    ctx.log("")

    if req.dry_run:
        ctx.stage("演练完成")
        result.ok = True
        result.output = str(target)
        result.log = ctx.text
        ctx.log("[dry-run] 未实际执行。")
        return result

    ctx.stage("正在修补镜像")
    code, output = run_stream(cmd, on_line=ctx.log, cwd=str(target.parent))
    if code != 0:
        tail = "\n".join(output.splitlines()[-6:])
        return _fail(ctx, result, f"ksud 执行失败（退出码 {code}）：\n{tail}")

    produced = parse_output_path(output)
    out_path = Path(produced) if produced else target
    if not out_path.is_file():
        return _fail(ctx, result, "ksud 报告成功，但没有找到输出的镜像文件。")

    result.output = str(out_path)
    result.out_size = out_path.stat().st_size

    # ---------- 7. 校验产物 ----------
    ctx.stage("校验产物")
    verify = boot_mod.parse_image(out_path, with_hash=False)
    if verify.error:
        ctx.log(f"  [警告] 产物解析失败：{verify.error}")
    else:
        ctx.log(f"  产物：{verify.kind_label} {verify.version_label}，{human_size(verify.size)}")
        if verify.already_patched:
            result.verified = True
            keys = [
                name
                for name in ("init", "init.real", "kernelsu.ko", "ksu_config")
                if name in verify.ramdisk_entries
            ]
            if keys:
                ctx.log("  [OK] ramdisk 已确认：" + "、".join(keys))
            else:
                ctx.log("  [OK] ramdisk 中已包含 kernelsu.ko")
            if verify.ksu_config:
                ctx.log(f"  ksu_config = {verify.ksu_config!r}")
                if req.allow_shell and "allow_shell=1" not in verify.ksu_config:
                    msg = "已勾选「允许 shell 永久 root」，但产物 ksu_config 里没有 allow_shell=1。"
                    result.warnings.append(msg)
                    ctx.log(f"  [警告] {msg}")
            elif req.allow_shell:
                msg = "产物里没有 ksu_config，allow_shell 未写入。"
                result.warnings.append(msg)
                ctx.log(f"  [警告] {msg}")
            if req.enable_adbd:
                ctx.log(
                    "  [提示] adb_debug.prop 已写入，但 KernelSU v3.x 内核不读取它，"
                    "adb root 请在管理器「设置 → ADB Root」里开启。"
                )
        elif verify.already_patched is False:
            msg = "产物 ramdisk 中未发现 kernelsu.ko，请勿刷入，并反馈本次日志。"
            result.warnings.append(msg)
            ctx.log(f"  [警告] {msg}")
        else:
            ctx.log("  [提示] 该 ramdisk 的压缩格式暂不支持解析，已跳过内容校验（以 ksud 输出为准）。")
        if verify.version != info.version:
            result.warnings.append("产物 header 版本与源镜像不一致，请谨慎刷入。")

    result.ok = True
    result.log = ctx.text
    ctx.stage("完成")
    ctx.log("")
    ctx.log(f"完成：{out_path}")
    return result
