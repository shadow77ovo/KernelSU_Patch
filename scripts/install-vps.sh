#!/bin/bash
# KernelSU LKM Web 部署脚本（Linux VPS）
# 用法：先 git clone 到服务器，然后在本目录执行 bash scripts/install-vps.sh

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/ksu-lkm-web}"
SERVICE_NAME="ksu-lkm-web"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="${PORT:-8848}"
PASSWORD="${PASSWORD:-}"

# 1. 基础检查
echo "==> 检查环境..."
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "错误：未找到 $PYTHON_BIN，请先安装 Python3"
    exit 1
fi
PY_VER=$($PYTHON_BIN -c "import sys; print(sys.version_info[:2] >= (3, 9))")
if [ "$PY_VER" != "True" ]; then
    echo "错误：需要 Python 3.9+"
    exit 1
fi

if ! command -v nginx >/dev/null 2>&1 && ! command -v caddy >/dev/null 2>&1; then
    echo "警告：未检测到 nginx/caddy。部署完成后需要手动配反向代理。"
fi

# 2. 复制项目到安装目录
echo "==> 安装到 $INSTALL_DIR ..."
sudo mkdir -p "$INSTALL_DIR"
sudo cp -r "$REPO_DIR"/* "$INSTALL_DIR/"
sudo chown -R "$(whoami):$(whoami)" "$INSTALL_DIR"

# 3. 创建 Python 虚拟环境
echo "==> 创建 venv..."
cd "$INSTALL_DIR"
$PYTHON_BIN -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel -q

# 没有第三方依赖（标准库即可），但如果以后有 requirements.txt 就装
if [ -f requirements.txt ]; then
    pip install -r requirements.txt -q
fi

# 4. 生成启动脚本
echo "==> 生成启动脚本..."
if [ -z "$PASSWORD" ]; then
    PASSWORD=$(python3 -c "import secrets,string;a=string.ascii_letters+string.digits;print('Ksu'+''.join(secrets.choice(a) for _ in range(12)))")
    echo "----------------------------------------"
    echo "随机生成访问密码：$PASSWORD"
    echo "----------------------------------------"
fi

cat > "$INSTALL_DIR/start.sh" <<EOF
#!/bin/bash
cd "$INSTALL_DIR"
source venv/bin/activate
export PYTHONUNBUFFERED=1
exec python web/server.py \\
    --host 127.0.0.1 \\
    --port $PORT \\
    --password "$PASSWORD" \\
    --allow-remote \\
    --no-browser
EOF
chmod +x "$INSTALL_DIR/start.sh"

# 5. 生成 systemd 服务
echo "==> 注册 systemd 服务..."
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=KernelSU LKM Web Patcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/start.sh
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$INSTALL_DIR/web/work $INSTALL_DIR/tools/ksud

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# 6. 预下载 ksud 引擎（避免第一次访问时慢）
echo "==> 预下载 ksud 引擎..."
cd "$INSTALL_DIR"
$PYTHON_BIN - <<'PY'
import sys, os
sys.path.insert(0, os.getcwd())
from ksulkm.downloader import download_engine
try:
    p = download_engine("latest", log=print)
    print(f"引擎已就绪：{p}")
except Exception as e:
    print(f"预下载失败（首次访问时会自动重试）：{e}")
PY

# 7. 启动
echo "==> 启动服务..."
sudo systemctl restart "$SERVICE_NAME"
sleep 2
if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "服务已启动：systemctl status $SERVICE_NAME"
else
    echo "启动失败，请查看日志：journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

# 8. 输出摘要
echo ""
echo "========================================"
echo "  KernelSU LKM Web 部署完成"
echo "========================================"
echo "  本地地址: http://127.0.0.1:$PORT"
echo "  访问密码: $PASSWORD"
echo "  安装目录: $INSTALL_DIR"
echo "  服务管理: sudo systemctl {start|stop|restart|status} $SERVICE_NAME"
echo "  查看日志: sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "下一步：配反向代理（nginx/Caddy/aaPanel）"
echo "========================================"
