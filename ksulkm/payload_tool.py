"""调用 payload.exe 从 OTA 全量包中提取 boot / init_boot。

payload.exe 是第三方小工具（支持本地 `payload.bin`，也支持 `--url` 直接从
云端只下载所需分区），这里只负责拼参数、解析输出、回报进度，不介入其内部逻辑。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .util import CREATE_NO_WINDOW, app_root, tools_dir

# 分区列表行，例如：  boot                 96.0 MB
PARTITION_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s+([\d.]+)\s*MB\s*$")
# 进度行，例如：  boot.img         [########------]  50.6%
PROGRESS_RE = re.compile(r"^\s*(\S+\.img)\s+\[[#\-\s]*\]\s*([\d.]+)\s*%")

# 分区优先级：boot 相关排前面
_PRIORITY = {"boot": 0, "init_boot": 1, "vendor_boot": 2, "recovery": 3, "dtbo": 4}


class PayloadToolError(RuntimeError):
    pass


def find_tool() -> Optional[Path]:
    """按顺序查找 payload.exe：tools/ → 项目根目录 → PATH。"""
    candidates = [
        tools_dir() / "payload.exe",
        app_root() / "payload.exe",
        app_root() / "tools" / "payload.exe",
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = Path(d) / "payload.exe"
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def is_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def build_args(tool: Path, source: str, partitions: list[str] | None = None,
               out_dir: str | Path | None = None, list_only: bool = False) -> list[str]:
    args = [str(tool)]
    if is_url(source):
        args += ["--url", source]
    else:
        args.append(str(Path(source).expanduser()))
    if list_only:
        args.append("--list")
    else:
        args += list(partitions or [])
        if out_dir:
            args += ["--out", str(out_dir)]
    return args


def list_partitions(source: str, timeout: int = 300) -> tuple[list[dict], str]:
    """列出可提取分区。云端 URL 需要先拉取 manifest，可能稍慢。"""
    tool = find_tool()
    if tool is None:
        raise PayloadToolError("没有找到 payload.exe（放到 tools/ 或项目根目录）")

    cmd = build_args(tool, source, list_only=True)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise PayloadToolError("扫描超时（网络太慢或包太大）") from None
    except OSError as exc:
        raise PayloadToolError(f"无法执行 payload.exe：{exc}") from None

    text = _decode(proc.stdout or b"")
    parts: list[dict] = []
    for line in text.splitlines():
        m = PARTITION_RE.match(line)
        if m:
            parts.append(
                {
                    "name": m.group(1),
                    "sizeMB": float(m.group(2)),
                    "sizeLabel": f"{m.group(2)} MB",
                }
            )

    if not parts:
        tail = "\n".join([ln for ln in text.splitlines() if ln.strip()][-4:])
        raise PayloadToolError(f"没有解析到分区列表。payload.exe 输出：\n{tail or '（空）'}")

    parts.sort(key=lambda x: (_PRIORITY.get(x["name"], 9), x["name"]))
    return parts, text


def extract(
    source: str,
    partitions: list[str],
    out_dir: str | Path,
    on_line: Callable[[str], None],
    on_progress: Optional[Callable[[float], None]] = None,
    timeout: int | None = None,
) -> int:
    """执行提取，逐行回调输出，进度按各分区百分比取平均。"""
    tool = find_tool()
    if tool is None:
        raise PayloadToolError("没有找到 payload.exe（放到 tools/ 或项目根目录）")

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    cmd = build_args(tool, source, partitions, out_dir)
    on_line("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None
    fd = proc.stdout.fileno()

    per_file: dict[str, float] = {}
    buf = b""
    while True:
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while True:
            m = re.search(rb"[\r\n]", buf)
            if not m:
                break
            raw, buf = buf[: m.start()], buf[m.end() :]
            line = _decode(raw).rstrip()
            if not line:
                continue
            pm = PROGRESS_RE.match(line)
            if pm:
                per_file[pm.group(1)] = float(pm.group(2))
                if on_progress and per_file:
                    avg = sum(per_file.values()) / max(len(partitions), len(per_file))
                    on_progress(min(avg / 100.0, 0.99))
                continue
            on_line(line)

    if buf.strip():
        on_line(_decode(buf).strip())

    proc.wait(timeout=timeout)
    return proc.returncode
