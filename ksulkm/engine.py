"""调用官方 ksud 引擎执行 boot-patch。

命令形态与 KernelSU App 内部完全一致（见 manager 的 KsuCli.kt），
PC 端额外补上 `--arch`，因为桌面版 ksud 需要知道给哪个架构的内核打补丁。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .util import CREATE_NO_WINDOW

LineFn = Callable[[str], None]

ARCH_ALIASES = {
    "arm64": "aarch64",
    "arm64-v8a": "aarch64",
    "aarch64": "aarch64",
    "arm": "aarch64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "amd64": "x86_64",
}


@dataclass
class EngineInfo:
    path: Path
    version: str = ""
    supports_boot_patch: bool = False
    ok: bool = False
    message: str = ""


def _run_capture(cmd: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def probe_engine(ksud: str | Path) -> EngineInfo:
    """确认这个可执行文件确实是能用的 ksud（而不是下载到一半的错误页）。"""
    p = Path(ksud)
    info = EngineInfo(path=p)
    if not p.exists():
        info.message = "引擎文件不存在"
        return info

    code, out = _run_capture([str(p), "--version"])
    if code == 0:
        info.version = out.strip().splitlines()[0] if out.strip() else ""
    else:
        # 某些版本没有 --version，用 --help 兜底
        code, out = _run_capture([str(p), "--help"])
        if code != 0:
            info.message = f"无法执行 ksud：{out.strip()[:200]}"
            return info

    code, out = _run_capture([str(p), "boot-patch", "--help"])
    if code == 0 and "--kmi" in out:
        info.supports_boot_patch = True
        info.ok = True
        info.message = "引擎可用"
    else:
        info.message = "该 ksud 不支持 boot-patch（可能不是官方 PC 版）"
    return info


def normalize_arch(arch: str) -> str:
    return ARCH_ALIASES.get((arch or "").strip().lower(), "aarch64")


def build_command(
    ksud: str | Path,
    image: str | Path,
    out_dir: str | Path,
    out_name: str,
    kmi: str = "",
    module: str | Path = "",
    arch: str = "aarch64",
    allow_shell: bool = False,
    enable_adbd: bool = False,
    block_modules: str = "",
) -> list[str]:
    """拼装 boot-patch 命令。

    注意：`--backup` / `--flash` / `--partition` / `-u` 是 Android 专用参数，
    PC 版 ksud 不接受，这里一律不传。
    """
    cmd: list[str] = [
        str(ksud),
        "boot-patch",
        "-b",
        str(Path(image).resolve()),
        "--out",
        str(Path(out_dir).resolve()),
        "--out-name",
        out_name,
        "--arch",
        normalize_arch(arch),
    ]
    if module:
        cmd += ["-m", str(Path(module).resolve())]
    elif kmi:
        cmd += ["--kmi", kmi]
    if allow_shell:
        cmd.append("--allow-shell")
    if enable_adbd:
        cmd.append("--enable-adbd")
    if block_modules:
        cmd += ["--block-module", block_modules]
    return cmd


def run_stream(cmd: list[str], on_line: LineFn, cwd: Optional[str] = None) -> tuple[int, str]:
    """执行命令并逐行回调输出，返回 (退出码, 完整输出)。"""
    env = dict(os.environ)
    env.setdefault("RUST_BACKTRACE", "0")
    # 让 Rust 端不要因为管道而缓冲输出
    env.setdefault("RUST_LOG", "info")

    collected: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=env,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        collected.append(line)
        on_line(line)
    proc.wait()
    return proc.returncode, "\n".join(collected)


def parse_output_path(output: str) -> str:
    """从 ksud 输出里取产物路径（"- Output file is written to" 的下一行）。"""
    lines = [ln.strip() for ln in output.splitlines()]
    for i, line in enumerate(lines):
        if "Output file is written to" in line:
            for nxt in lines[i + 1 : i + 4]:
                nxt = nxt.lstrip("- ").strip().strip('"')
                if nxt and (nxt.endswith(".img") or os.path.isabs(nxt)):
                    return nxt
    return ""
