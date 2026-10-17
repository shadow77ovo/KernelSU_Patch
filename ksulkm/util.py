"""通用小工具：大小格式化、哈希、路径处理、子进程窗口抑制。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

# Windows 下调用控制台程序时不弹出黑框
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 统一的日志回调类型：接收一行文本
LogFn = "callable"


def human_size(num: float) -> str:
    """把字节数格式化成人类可读的字符串。"""
    if num is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def sha256_file(path: str | os.PathLike, chunk: int = 1 << 20) -> str:
    """计算文件 SHA-256（分块读取，避免大镜像占内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_dir(path: str | os.PathLike) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def unique_path(path: str | os.PathLike) -> Path:
    """若目标已存在，自动追加 -1 / -2 …，避免覆盖任何已有文件。"""
    p = Path(path)
    if not p.exists():
        return p
    stem, suffix, parent = p.stem, p.suffix, p.parent
    i = 1
    while True:
        cand = parent / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def default_out_name(image_path: str | os.PathLike) -> str:
    """根据源镜像名生成输出文件名，例如 boot.img -> boot-ksu-patched.img。"""
    stem = Path(image_path).stem or "boot"
    return f"{stem}-ksu-patched.img"


def app_root() -> Path:
    """项目根目录（兼容 PyInstaller 打包后的运行方式）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def tools_dir() -> Path:
    return ensure_dir(app_root() / "tools")


def pick_file_dialog(title: str = "选择文件", filter_str: str = "") -> str:
    """在 Windows 上弹出原生文件选择框（服务端本机），返回路径；取消返回 ""。

    网页里没法直接拿到本地路径，但后端就跑在本机，所以由后端弹框最省事。
    非 Windows 直接返回 ""，前端会退化成手动填写路径。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD),
                ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE),
                ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR),
                ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD),
                ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD),
                ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD),
                ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR),
                ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD),
                ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR),
                ("lCustData", wintypes.LPARAM),
                ("lpfnHook", wintypes.LPVOID),
                ("lpTemplateName", wintypes.LPCWSTR),
            ]

        buf = ctypes.create_unicode_buffer(4096)
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        ofn.nMaxFile = 4096
        ofn.lpstrTitle = title
        ofn.lpstrFilter = filter_str or "所有文件\0*.*\0\0"
        # OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_EXPLORER
        ofn.Flags = 0x00001000 | 0x00000800 | 0x00000008 | 0x00080000

        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return buf.value
    except Exception:  # noqa: BLE001
        return ""
    return ""


# ------------------------------------------------------------------ 桌面目录
def _desktop_from_known_folder() -> "Path | None":
    """用 SHGetKnownFolderPath 取当前用户桌面（能正确处理 OneDrive 重定向、改名、多用户）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        folder_id = GUID(
            0xB4BFCC3A,
            0xDB2C,
            0x424C,
            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        ptr = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(ptr)
        )
        if hr == 0 and ptr.value:
            value = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return Path(value)
    except Exception:  # noqa: BLE001
        return None
    return None


def _desktop_from_registry() -> "Path | None":
    r"""读注册表里的 User Shell Folders\Desktop（可能是 %USERPROFILE%\Desktop 或 OneDrive 路径）。"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        try:
            raw, _ = winreg.QueryValueEx(key, "Desktop")
        finally:
            winreg.CloseKey(key)
        return Path(os.path.expandvars(raw))
    except Exception:  # noqa: BLE001
        return None


def desktop_dir() -> "Path | None":
    """返回当前登录用户的桌面目录；解析不到返回 None。

    优先用系统 API，其次注册表，最后按常见位置兜底（含 OneDrive 桌面）。
    这样换一个用户、桌面被 OneDrive 接管、或者用户名不是 Ricardo 都能正确落盘。
    """
    if os.name == "nt":
        for resolver in (_desktop_from_known_folder, _desktop_from_registry):
            p = resolver()
            if p and p.is_dir():
                return p
        for cand in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop", Path.home() / "桌面"):
            if cand.is_dir():
                return cand
        return None

    xdg = os.environ.get("XDG_DESKTOP_DIR")
    if xdg:
        p = Path(os.path.expandvars(xdg))
        if p.is_dir():
            return p
    for cand in (Path.home() / "Desktop", Path.home() / "桌面"):
        if cand.is_dir():
            return cand
    return None


def copy_to_desktop(src: str | os.PathLike) -> "Path | None":
    """把文件复制到桌面（重名自动加 -1/-2），返回落地路径；桌面不可用返回 None。"""
    desk = desktop_dir()
    if desk is None:
        return None
    source = Path(src)
    try:
        dest = unique_path(desk / source.name)
        shutil.copy2(source, dest)
        return dest
    except OSError:
        return None
