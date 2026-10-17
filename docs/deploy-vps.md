# VPS / 云服务器部署指南

把服务部署到你购买的 Linux 服务器上，这样**不需要家里电脑 24 小时开机**，也能随时随地打补丁。

> ⚠️ Linux 限制：`payload.exe` 没有 Linux 版，因此 **OTA 全量包云提取功能不可用**。你仍然可以上传本地的 `boot.img` / `init_boot.img` / `KernelSU.apk` 进行修补，这是最常见的用法，不影响核心功能。

---

## 前置要求

| 项目 | 要求 |
|---|---|
| 系统 | Linux（Ubuntu / Debian / CentOS / Alma 均可，x86_64） |
| Python | 3.9+ |
| 内存 | ≥ 512MB（实际峰值 < 200MB） |
| 磁盘 | ≥ 2GB 临时空间（镜像解包用） |
| 网络 | 能访问 GitHub（下载 ksud 引擎） |
| 面板 | 有 aaPanel / 宝塔、或能改 nginx 配置 |

---

## 方案一：一键脚本（推荐）

### 1. 把代码传到服务器

```bash
# 在你电脑上先 commit + push，然后在服务器上
git clone https://github.com/你的用户名/你的仓库.git /opt/ksu-lkm-web
cd /opt/ksu-lkm-web
```

或者直接用 `scp` / `sftp` 上传整个文件夹。

### 2. 执行安装脚本

```bash
cd /opt/ksu-lkm-web
export PASSWORD="你想设的强密码"   # 可选，不设会随机生成
bash scripts/install-vps.sh
```

脚本会自动完成：
- 创建 Python venv
- 注册 systemd 开机自启服务
- 预下载 Linux 版 `ksud` 引擎
- 启动服务（监听 `127.0.0.1:8848`）

### 3. 配反向代理

服务只监听了本地回环，需要 nginx / Caddy / aaPanel 反代出去。

#### aaPanel / 宝塔 用户

1. 登录你的面板（如 `https://192.140.189.123:38959/cb376884`）
2. 「网站」→ 添加站点 → 域名填 `ksu.你的域名.com`（或直接用 IP）
3. 点开站点 → 「配置文件」
4. 在 `location / { ... }` **之前**插入 `scripts/nginx-ksu.conf` 里的内容（从 `location / {` 开始到 `}` 结束）
5. 保存，然后「服务」→ 重载 nginx

#### 纯 nginx 用户

```bash
sudo cp scripts/nginx-ksu.conf /etc/nginx/conf.d/ksu.conf
sudo nginx -t && sudo systemctl reload nginx
```

#### Caddy 用户

```
ksu.你的域名.com {
    reverse_proxy 127.0.0.1:8848
}
```

### 4. 访问测试

浏览器打开 `https://ksu.你的域名.com`（或 `http://服务器IP`），会弹出 Basic 登录框：
- 用户名：随便填（如 `ksu`）
- 密码：`install-vps.sh` 输出那串（或你设的 `PASSWORD`）

---

## 方案二：手动部署（如果你不想用脚本）

```bash
# 1) 上传代码到 /opt/ksu-lkm-web

# 2) 创建虚拟环境
cd /opt/ksu-lkm-web
python3 -m venv venv
source venv/bin/activate

# 3) 启动（带密码 + 仅本地监听）
python web/server.py \
    --host 127.0.0.1 \
    --port 8848 \
    --password "你的强密码" \
    --allow-remote \
    --no-browser

# 4) 另开 tmux / screen，或用 systemd 保活
```

---

## 日常管理

```bash
# 查看状态
sudo systemctl status ksu-lkm-web

# 重启
sudo systemctl restart ksu-lkm-web

# 看实时日志
sudo journalctl -u ksu-lkm-web -f

# 停止
sudo systemctl stop ksu-lkm-web

# 开机自启（install 脚本已经做了）
sudo systemctl enable ksu-lkm-web
```

---

## 安全建议

1. **务必设密码**：`--password` 不能空，否则任何人都能往你服务器上传文件。
2. **不要直接暴露 8848**：只让 nginx 反代 `127.0.0.1:8848`，防火墙封掉外部对 8848 的访问。
3. **aaPanel 自带防火墙**：记得在「安全」→ 放行端口里，只放 80/443，不放 8848。
4. **HTTPS**：aaPanel 可以一键申请 Let's Encrypt，或者套 Cloudflare CDN。

---

## 卸载

```bash
sudo systemctl stop ksu-lkm-web
sudo systemctl disable ksu-lkm-web
sudo rm /etc/systemd/system/ksu-lkm-web.service
sudo rm -rf /opt/ksu-lkm-web
sudo systemctl daemon-reload
```
