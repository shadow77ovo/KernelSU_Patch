"""官方 ksud（PC 版）引擎的获取与缓存。

KernelSU 每个 release 都会附带一份 `ksud-x86_64-pc-windows-gnu.exe`，
它内部已经打包了 ksuinit 与全部 `<kmi>_kernelsu.ko`，可以直接在电脑上
对 boot / init_boot / vendor_boot 镜像执行 `boot-patch`。
"""

from __future__ import annotations

import json
import os
import platform
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .util import ensure_dir, human_size

OWNER = "tiann"
REPO = "KernelSU"
def engine_asset() -> str:
    """按当前平台挑选官方提供的 ksud 引擎。"""
    if sys.platform == "win32":
        return "ksud-x86_64-pc-windows-gnu.exe"
    if sys.platform.startswith("linux"):
        return "ksud-x86_64-unknown-linux-musl"
    if sys.platform == "darwin":
        if platform.machine().lower() in ("arm64", "aarch64"):
            return "ksud-aarch64-apple-darwin"
        return "ksud-x86_64-apple-darwin"
    raise DownloadError(f"暂不支持的平台：{sys.platform}")


def engine_platform_tag() -> str:
    if sys.platform == "win32":
        return "windows-x86_64.exe"
    if sys.platform.startswith("linux"):
        return "linux-x86_64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "macos-x86_64"
    return "unknown"

# GitHub 直连经常超时，按顺序尝试镜像前缀（前缀 + 原始 URL）
MIRROR_PREFIXES = [
    "",
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://mirror.ghproxy.com/",
    "https://gh.llkk.cc/",
]

USER_AGENT = "ksu-lkm-patcher/1.0 (+https://github.com/tiann/KernelSU)"
MIN_ENGINE_SIZE = 3 * 1024 * 1024  # 正常在 8MB 左右，低于 3MB 必然是错误页面

ProgressFn = Callable[[int, int, str], None]
LogFn = Callable[[str], None]


class DownloadError(RuntimeError):
    pass


def engine_cache_path(tag: str) -> Path:
    from .util import tools_dir

    return tools_dir() / "ksud" / f"ksud-{tag}-{engine_platform_tag()}"


def is_valid_engine(path: Path) -> bool:
    """粗略校验：可执行文件头（PE / ELF）+ 合理体积。"""
    try:
        if not path.is_file() or path.stat().st_size < MIN_ENGINE_SIZE:
            return False
        with open(path, "rb") as f:
            head = f.read(4)
        return head[:2] == b"MZ" or head == b"\x7fELF"
    except Exception:  # noqa: BLE001
        return False


def _download_once(url: str, dest: Path, progress: Optional[ProgressFn]) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    context = ssl.create_default_context()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=30, context=context) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, url)
    if tmp.stat().st_size < MIN_ENGINE_SIZE:
        size = tmp.stat().st_size
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"下载内容异常（仅 {human_size(size)}），可能是错误页或被拦截")
    tmp.replace(dest)
    if os.name != "nt":
        dest.chmod(0o755)


def _github_latest_tag() -> str:
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("tag_name", ""))


def download_engine(
    tag: str,
    progress: Optional[ProgressFn] = None,
    log: Optional[LogFn] = None,
) -> Path:
    """下载指定 tag 的官方 ksud 引擎，返回本地路径。"""
    log = log or (lambda _m: None)
    dest = engine_cache_path(tag)
    ensure_dir(dest.parent)

    direct = f"https://github.com/{OWNER}/{REPO}/releases/download/{tag}/{engine_asset()}"
    errors: list[str] = []

    for prefix in MIRROR_PREFIXES:
        url = prefix + direct
        label = prefix or "GitHub 直连"
        try:
            log(f"正在下载 ksud 引擎（{label}）…")
            _download_once(url, dest, progress)
            if is_valid_engine(dest):
                log(f"ksud 引擎已就绪：{dest}")
                return dest
            dest.unlink(missing_ok=True)
            errors.append(f"{label}: 文件校验失败")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, DownloadError) as exc:
            errors.append(f"{label}: {exc}")
            log(f"  {label} 失败：{exc}")
            continue

    detail = "\n  ".join(errors)
    raise DownloadError(
        f"无法下载 {tag} 版本的 ksud 引擎。\n  {detail}\n"
        "可以手动下载后放到 tools/ 目录：\n"
        f"  {direct}\n"
        "然后在界面上用「引擎路径」指定它。"
    )


def ensure_engine(
    apk_version: str,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
) -> Path:
    """确保存在与 APK 版本匹配的 ksud 引擎，返回其路径。

    匹配顺序：
      1. tools/ksud/ 下已有对应 tag 的缓存
      2. tools/ksud.exe（用户手动放置）
      3. 按 APK 版本下载；版本没有对应 release 时回退到 latest
    """
    log = log or (lambda _m: None)
    from .util import tools_dir

    tag = apk_version if apk_version.startswith("v") else f"v{apk_version}"
    cached = engine_cache_path(tag)
    if is_valid_engine(cached):
        log(f"使用已缓存的 ksud 引擎（{tag}）")
        return cached

    manual = tools_dir() / "ksud.exe"
    if is_valid_engine(manual):
        log(f"使用手动放置的 ksud 引擎：{manual}")
        return manual

    try:
        return download_engine(tag, progress=progress, log=log)
    except DownloadError as first_error:
        # 版本可能来自 dev 构建，没有对应 release，退回最新版
        try:
            latest = _github_latest_tag()
        except Exception:  # noqa: BLE001
            raise first_error from None
        if not latest or latest == tag:
            raise first_error
        log(f"未找到 {tag} 对应的 release，改用最新版 {latest}")
        cached = engine_cache_path(latest)
        if is_valid_engine(cached):
            return cached
        return download_engine(latest, progress=progress, log=log)
