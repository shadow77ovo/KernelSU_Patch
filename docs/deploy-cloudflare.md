# 部署到 Cloudflare + 绑定自己的域名

## 先说结论

**这个项目不能直接部署到 Cloudflare Pages / Workers。**

原因很具体，不是配置问题：

| 项目需要 | Cloudflare Pages/Workers | 结果 |
|---|---|---|
| 执行 `ksud.exe` / `payload.exe` 这类本机可执行文件 | Workers 里是 V8 / Pyodide（WASM），**不能 spawn 进程** | ❌ |
| 读写本地磁盘（解包镜像、写回产物） | Workers 没有文件系统 | ❌ |
| 上传 64~96MB 甚至几 GB 的镜像 | Workers 请求体上限 100MB（免费/Pro） | ❌ |
| 长时间 CPU 计算（重打包 64MB 镜像） | Workers 有 CPU 时间上限 | ❌ |

所以：**计算必须留在你自己的电脑（或一台 VPS）上**，Cloudflare 只负责「域名 + HTTPS + 转发」。
好消息是这完全做得到，而且免费。

---

## 方案 A：Cloudflare Tunnel（推荐，免费，零改造）

效果：`https://ksu.你的域名.com` → 自动转发到你电脑上的 `http://127.0.0.1:8848`。
不用公网 IP、不用开端口、不用买服务器，Cloudflare 自动签 HTTPS 证书。

### 前提

- 你的域名已经托管在 Cloudflare（NS 指向 Cloudflare）
- 电脑保持开机（睡眠/关机就访问不了）

### 步骤

**1. 下载 cloudflared**

到 <https://github.com/cloudflare/cloudflared/releases/latest> 下载
`cloudflared-windows-amd64.exe`，改名为 `cloudflared.exe`，放到 `C:\cloudflared\`。

**2. 登录并授权域名**

```bat
C:\cloudflared\cloudflared.exe tunnel login
```

浏览器会打开授权页，选中你的域名 → 授权。成功后本地多出 `%USERPROFILE%\.cloudflared\cert.pem`。

**3. 创建隧道**

```bat
C:\cloudflared\cloudflared.exe tunnel create ksu
```

记下输出的 **Tunnel ID**（一串 UUID），并确认
`%USERPROFILE%\.cloudflared\<Tunnel ID>.json` 已生成。

**4. 写配置文件**

新建 `%USERPROFILE%\.cloudflared\config.yml`：

```yaml
tunnel: <把 Tunnel ID 填这里>
credentials-file: C:\Users\<你的用户名>\.cloudflared\<Tunnel ID>.json

ingress:
  - hostname: ksu.你的域名.com
    service: http://127.0.0.1:8848
  - service: http_status:404
```

**5. 绑定域名（自动加 DNS 记录）**

```bat
C:\cloudflared\cloudflared.exe tunnel route dns ksu ksu.你的域名.com
```

**6. 启动**

先开本地服务（**一定要带密码**）：

```bat
python web\server.py --password 换成你自己的强密码
```

再开隧道：

```bat
C:\cloudflared\cloudflared.exe tunnel run ksu
```

浏览器访问 `https://ksu.你的域名.com`，会先弹一次登录框（用户名随便填，密码是上面设的）。

**7. 装成开机自启服务（可选）**

```bat
C:\cloudflared\cloudflared.exe service install
```

它会把 config.yml 里的隧道注册成 Windows 服务，开机自动连。

---

## 方案 B：VPS + Cloudflare 代理

如果你希望**电脑关机也能用**，租一台 Linux VPS（1核2G 足够）：

```bash
# 1. 上传项目（不含 *.img / payload.exe 之类的大文件）
git clone https://github.com/你/你的仓库.git ksu && cd ksu

# 2. 启动（Linux 会自动下载 ksud-x86_64-unknown-linux-musl）
python3 web/server.py --host 127.0.0.1 --port 8848 --password 你的密码
```

前面套一个 Caddy 或 Nginx 反代到 8848，然后在 Cloudflare 里把 `ksu.你的域名.com`
的 A 记录指向 VPS IP、打开小云朵（Proxied）即可。

> Linux 上的「本地 payload.bin」和「浏览…」按钮不可用（原生文件对话框只有 Windows 有），
> 云端提取和本地上传镜像都正常。

---

## ⚠️ 上线前必须知道的三件事

**1. 一定要设密码**

`--password` 没设的话，任何能访问这个地址的人都能往你电脑上传镜像、触发修补。
设了之后浏览器会弹标准登录框，接口也一并受保护。

**2. 强烈建议再套一层 Cloudflare Access（免费）**

Cloudflare 控制台 → Zero Trust → Access → Applications → Add an application → Self-hosted：

- Application domain：`ksu.你的域名.com`
- Policy：Action = Allow，Include = Emails = 你的邮箱
- 登录方式选 One-time PIN（邮箱收验证码）

这样即使密码泄露，陌生人也进不来。

**3. Cloudflare 代理有 100MB 请求体上限（免费/Pro 套餐）**

- 上传 `boot.img`（常见 64~96MB）没问题，但**超过 100MB 的镜像会失败**；
- 解决办法：改用「云提取」——那是服务器自己去 OTA 拉数据，不经过浏览器上传，没有这个限制；
- 本地的 `payload.bin` 走的是**路径**而不是上传，也不受限制（但只有 Windows 本机可用）。

---

## 安全清单（照着打勾）

- [ ] `--password` 已设置，且不是弱密码
- [ ] 已开启 Cloudflare Access（邮箱验证）
- [ ] 不需要时把隧道停掉（`cloudflared tunnel run` 的窗口关掉即可）
- [ ] 不要把 `payload.exe`、厂商固件 `*.img`、APK 提交进 Git 仓库（见 README 的仓库卫生说明）
- [ ] 桌面产物用完自己清理，避免长期堆积
