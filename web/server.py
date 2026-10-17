"""KernelSU LKM 修补工具 —— 网页版后端。

设计：浏览器只做界面，真正打补丁的还是本机的官方 ksud 引擎。
默认只监听 127.0.0.1，镜像不出本机；也可以部署到 Linux 服务器
（会自动下载 linux-musl 版 ksud）。

启动：
    python web/server.py                 # 打开 http://127.0.0.1:8848
    python web/server.py --port 9000
    python web/server.py --host 0.0.0.0 --allow-remote   # 对外提供服务（有风险，见 README）
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ksulkm import apk as apk_mod  # noqa: E402
from ksulkm import payload_tool  # noqa: E402
from ksulkm import bootimg as boot_mod  # noqa: E402
from ksulkm.downloader import engine_cache_path, is_valid_engine  # noqa: E402
from ksulkm.patcher import PatchRequest, patch  # noqa: E402
from ksulkm.util import copy_to_desktop, desktop_dir, human_size, pick_file_dialog  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
WORK_DIR = WEB_DIR / "work"
MAX_UPLOAD = 2 * 1024 ** 3  # 2GB 上限
AUTH_PASSWORD = ""  # 由 --password 设置；为空表示不校验
SID_RE = re.compile(r"^[0-9a-f]{8,64}$")

# 进程内保存：下载令牌 -> 文件路径；job id -> Job
DOWNLOADS: dict[str, Path] = {}
JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()


# ------------------------------------------------------------------ 数据模型
@dataclass
class Job:
    id: str
    status: str = "running"  # running | done | error
    stage: str = "准备中"
    progress: float = 0.0
    log: list[str] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, line: str) -> None:
        with self.lock:
            self.log.append(line)

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "status": self.status,
                "stage": self.stage,
                "progress": round(self.progress, 3),
                "log": self.log[since:],
                "total": len(self.log),
                "result": self.result,
                "error": self.error,
            }


def _image_dict(info: boot_mod.BootImageInfo) -> dict:
    return {
        "error": info.error,
        "kind": info.kind,
        "kindLabel": info.kind_label,
        "version": info.version,
        "pageSize": info.page_size,
        "size": info.size,
        "sizeLabel": human_size(info.size),
        "ramdiskSize": info.ramdisk_size,
        "ramdiskLabel": human_size(info.ramdisk_size) if info.ramdisk_size else "无",
        "compression": info.ramdisk_compression,
        "kernelVersion": info.kernel_version,
        "kmi": info.kmi,
        "avb": info.avb,
        "patched": info.already_patched,
        "supported": info.supported_by_engine,
        "warnings": info.warnings,
        "ksuConfig": info.ksu_config,
    }


def _apk_dict(info: apk_mod.ApkInfo) -> dict:
    return {
        "error": info.error,
        "version": info.version_name,
        "versionCode": info.version_code,
        "abis": info.abis,
        "kmis": info.kmis,
        "size": info.size,
        "sizeLabel": human_size(info.size),
    }


# ------------------------------------------------------------------ 任务执行
def run_job(job: Job, req: PatchRequest) -> None:
    try:
        result = patch(
            req,
            on_log=job.add,
            on_stage=lambda m: setattr(job, "stage", m),
        )
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"未预期异常：{exc}"
        job.add(f"[错误] {job.error}")
        return

    if not result.ok:
        job.status = "error"
        job.error = result.error
        return

    out = Path(result.output)
    token = uuid.uuid4().hex
    with _LOCK:
        DOWNLOADS[token] = out

    on_desk = copy_to_desktop(out)
    if on_desk:
        job.add(f"[OK] 已保存到桌面：{on_desk}")

    job.status = "done"
    job.stage = "完成"
    job.result = {
        "name": (on_desk or out).name,
        "path": str(on_desk or out),
        "onDesktop": bool(on_desk),
        "size": result.out_size,
        "sizeLabel": human_size(result.out_size),
        "download": f"/api/download/{token}",
        "verified": result.verified,
        "ksuConfig": (result.log and ""),
        "warnings": result.warnings,
        "partition": "init_boot" if result.image_info and result.image_info.kind == "init_boot" else "boot",
    }


def run_extract(job: Job, source: str, partitions: list[str], out_dir: Path) -> None:
    """后台执行 payload.exe 提取。"""
    try:
        job.stage = "提取中"
        code = payload_tool.extract(
            source,
            partitions,
            out_dir,
            on_line=job.add,
            on_progress=lambda v: setattr(job, "progress", v),
        )
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = str(exc)
        job.add(f"[错误] {exc}")
        return

    if code != 0:
        job.status = "error"
        job.error = f"payload.exe 退出码 {code}，请看上面的输出"
        return

    picked = None
    for want in partitions:
        for cand in (out_dir / f"{want}.img", out_dir / want):
            if cand.is_file():
                picked = cand
                break
        if picked:
            break
    if picked is None:
        job.status = "error"
        job.error = "提取结束但没有找到输出镜像"
        return

    on_desk = copy_to_desktop(picked)
    if on_desk:
        job.add(f"[OK] 已保存到桌面：{on_desk}")

    job.progress = 1.0
    job.status = "done"
    job.stage = "提取完成"
    job.result = {
        "name": (on_desk or picked).name,
        "size": picked.stat().st_size,
        "sizeLabel": human_size(picked.stat().st_size),
        "path": str(on_desk or picked),
        "onDesktop": bool(on_desk),
    }


def _ota_source(body: dict) -> str:
    src = (body.get("source") or "").strip().strip('"')
    if not src:
        raise ValueError("请填写 OTA 全量包地址，或选择本地 payload.bin")
    if payload_tool.is_url(src):
        return src
    path = Path(src).expanduser()
    if not path.is_file():
        raise ValueError(f"本地文件不存在：{path}")
    return str(path)


# ------------------------------------------------------------------ HTTP 层
class Handler(BaseHTTPRequestHandler):
    server_version = "ksu-lkm-web/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 鉴权 ----
    def _auth_ok(self) -> bool:
        if not AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False
        _user, _, password = raw.partition(":")
        return hmac.compare_digest(password, AUTH_PASSWORD)

    def _require_auth(self) -> bool:
        """返回 True 表示已通过；否则已回 401。"""
        if self._auth_ok():
            return True
        self.close_connection = True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="KernelSU LKM", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        return False

    # ---- 基础响应 ----
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_UPLOAD:
            raise ValueError("请求体过大")
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _json_body(self) -> dict:
        raw = self._body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_file(STATIC_DIR / "index.html")
            if path.startswith("/static/"):
                name = path[len("/static/") :]
                if "/" in name or "\\" in name or ".." in name:
                    return self._error("非法路径", 400)
                return self._serve_file(STATIC_DIR / name)
            if path == "/api/engine":
                return self._json(self._engine_state())
            if path == "/api/ota/browse":
                if os.name != "nt":
                    return self._error("仅 Windows 支持弹出选择框，请手动填写路径", 501)
                picked = pick_file_dialog(
                    "选择 OTA 全量包（.zip）或 payload.bin",
                    "固件包\0*.zip;*.bin\0所有文件\0*.*\0\0",
                )
                return self._json({"path": picked})
            if path.startswith("/api/job/"):
                jid = path.rsplit("/", 1)[-1]
                job = JOBS.get(jid)
                if not job:
                    return self._error("任务不存在", 404)
                since = 0
                qs = urlparse(self.path).query
                for part in qs.split("&"):
                    if part.startswith("since="):
                        since = int(part[6:] or 0)
                return self._json(job.snapshot(since))
            if path.startswith("/api/download/"):
                token = path.rsplit("/", 1)[-1]
                target = DOWNLOADS.get(token)
                if not target or not target.is_file():
                    return self._error("文件不存在或已过期", 404)
                return self._serve_download(target)
            return self._error("Not Found", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(f"服务器内部错误：{exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/upload":
                return self._handle_upload()
            if path == "/api/inspect":
                return self._handle_inspect()
            if path == "/api/patch":
                return self._handle_patch()
            if path == "/api/ota/list":
                return self._handle_ota_list()
            if path == "/api/ota/extract":
                return self._handle_ota_extract()
            return self._error("Not Found", 404)
        except ValueError as exc:
            self._error(str(exc), 400)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(f"服务器内部错误：{exc}", 500)

    # ---- 静态 / 下载 ----
    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            return self._error("Not Found", 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        if path.suffix in (".html", ".css"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def _serve_download(self, path: Path) -> None:
        size = path.stat().st_size
        fname = path.name
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{fname}\"; filename*=UTF-8''{quote(fname)}",
        )
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ---- 业务 ----
    def _engine_state(self) -> dict:
        from ksulkm.downloader import engine_platform_tag

        hits = sorted((ROOT / "tools" / "ksud").glob("ksud-*"))
        valid = [p for p in hits if is_valid_engine(p)]
        tool = payload_tool.find_tool()
        return {
            "platform": engine_platform_tag(),
            "cached": [p.name for p in valid],
            "ready": bool(valid),
            "payloadTool": str(tool) if tool else "",
        }

    def _session_dir(self, create: bool = False) -> Path | None:
        sid = self.headers.get("X-Session", "").strip()
        if not SID_RE.match(sid):
            return None
        d = WORK_DIR / sid
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _handle_upload(self) -> None:
        sdir = self._session_dir(create=True)
        if sdir is None:
            return self._error("缺少或非法的会话标识", 400)

        raw_name = unquote(self.headers.get("X-File-Name", "upload.bin"))
        name = os.path.basename(raw_name.replace("\\", "/")).strip() or "upload.bin"
        name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)[:120]

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._error("空文件", 400)
        if length > MAX_UPLOAD:
            return self._error("文件过大（上限 2GB）", 413)

        dest = sdir / name
        remaining = length
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)

        self._json({"name": name, "size": dest.stat().st_size, "sizeLabel": human_size(dest.stat().st_size)})

    def _handle_inspect(self) -> None:
        sdir = self._session_dir()
        if sdir is None:
            return self._error("缺少或非法的会话标识", 400)
        body = self._json_body()
        out: dict = {}
        img = body.get("image")
        if img:
            p = sdir / os.path.basename(img)
            out["image"] = _image_dict(boot_mod.parse_image(p)) if p.is_file() else {"error": "镜像不存在"}
        apk_name = body.get("apk")
        if apk_name:
            p = sdir / os.path.basename(apk_name)
            out["apk"] = _apk_dict(apk_mod.parse_apk(p)) if p.is_file() else {"error": "APK 不存在"}
        self._json(out)

    def _handle_patch(self) -> None:
        sdir = self._session_dir()
        if sdir is None:
            return self._error("缺少或非法的会话标识", 400)
        body = self._json_body()

        image = sdir / os.path.basename(body.get("image") or "")
        apk_path = sdir / os.path.basename(body.get("apk") or "")
        if not image.is_file():
            return self._error("请先上传镜像", 400)
        if not apk_path.is_file():
            return self._error("请先上传 KernelSU APK", 400)

        out_dir = sdir / "out"
        out_dir.mkdir(exist_ok=True)

        req = PatchRequest(
            image=str(image),
            apk=str(apk_path),
            out_dir=str(out_dir),
            out_name=(body.get("outName") or "").strip(),
            kmi=(body.get("kmi") or "").strip(),
            arch=(body.get("arch") or "aarch64").strip(),
            allow_shell=bool(body.get("allowShell")),
            enable_adbd=False,
        )
        job = Job(id=uuid.uuid4().hex)
        with _LOCK:
            JOBS[job.id] = job
        threading.Thread(target=run_job, args=(job, req), daemon=True).start()
        self._json({"job": job.id})


    def _handle_ota_list(self) -> None:
        body = self._json_body()
        source = _ota_source(body)
        try:
            parts, _raw = payload_tool.list_partitions(source)
        except payload_tool.PayloadToolError as exc:
            return self._error(str(exc), 502)
        self._json({"partitions": parts, "source": source})

    def _handle_ota_extract(self) -> None:
        sdir = self._session_dir()
        if sdir is None:
            return self._error("缺少或非法的会话标识", 400)
        body = self._json_body()
        source = _ota_source(body)
        partitions = [p for p in (body.get("partitions") or []) if p]
        if not partitions:
            return self._error("请先选择要提取的分区", 400)

        job = Job(id=uuid.uuid4().hex)
        with _LOCK:
            JOBS[job.id] = job
        threading.Thread(
            target=run_extract, args=(job, source, partitions, sdir), daemon=True
        ).start()
        self._json({"job": job.id})


# ------------------------------------------------------------------ 启动
def _port_in_use(host: str, port: int) -> bool:
    """端口是否已被占用。

    Windows 的 SO_REUSEADDR 允许两个进程同时 bind 同一个端口，后起的那个
    会静默失败、请求仍然被旧进程接走（表现为「改了代码却不生效」）。
    所以启动前先主动探测一次，占用就明确报错。
    """
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((probe, port), timeout=0.5):
            return True
    except OSError:
        return False


def _prepare_workdir() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # 清理 12 小时前的临时目录
    cutoff = time.time() - 12 * 3600
    for child in WORK_DIR.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="KernelSU LKM 修补工具 · 网页版")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8848, help="端口，默认 8848")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--allow-remote", action="store_true", help="允许监听非本机地址")
    parser.add_argument(
        "--password",
        default="",
        help="访问密码（对公网开放时务必设置；浏览器会弹一次登录框）",
    )
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        print("[拒绝启动] 监听公网地址需要显式加 --allow-remote。")
        print("           任何人都能往这台机器上传镜像并触发补丁，请先读 README 的风险说明。")
        return 2

    if _port_in_use(args.host, args.port):
        print(f"[拒绝启动] 端口 {args.port} 已被占用。")
        print("           多半是已经开着一个窗口了：先关掉它，或换端口 —— python web/server.py --port 8849")
        return 2

    global AUTH_PASSWORD
    AUTH_PASSWORD = args.password.strip()

    _prepare_workdir()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print("=" * 58)
    print(" KernelSU LKM 修补工具 · 网页版")
    print(f" 地址：{url}")
    print(" 镜像只在本机处理，不会上传到任何服务器")
    _desk = desktop_dir()
    print(f" 产物保存到桌面：{_desk if _desk else '（未找到桌面目录，仅保存在 web/work）'}")
    if AUTH_PASSWORD:
        print(" 访问密码：已开启（浏览器会要求登录）")
    elif args.host not in ("127.0.0.1", "localhost", "::1"):
        print(" ⚠ 未设置访问密码，任何能访问该端口的人都能上传镜像并触发修补！")
    print(" 按 Ctrl+C 停止")
    print("=" * 58)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
