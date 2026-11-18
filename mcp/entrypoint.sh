#!/bin/bash
# =============================================================
# entrypoint.sh - chrome-cdp 启动脚本
# =============================================================
# 启动顺序：
#   1. 清理残留锁
#   2. 启动 Xvfb 虚拟显示
#   3. 启动 x11vnc（VNC 服务）
#   4. 启动 noVNC（Web VNC）
#   5. 启动 Chrome（X11 模式 + CDP 9222）
# =============================================================
# 2026-09-19 健壮化：
#   - 每个后台进程在启动处立即捕获自己的 PID（原脚本 socat 的 PID 被错位覆盖）
#   - x11vnc / websockify / socat 增加存活检查（原先只有 Xvfb / Chrome 有）
#   - CDP 探测 5 次失败即 exit 1，交给 restart 策略重启（原先静默继续，产生"运行中但已坏"容器）
#   - opencli 启动器 disown，避免其快速退出被 wait -n 误判为子进程死亡而重启风暴
#   - cleanup 覆盖 opencli 守护进程（原先漏杀，socat 的 PID 也从未正确捕获）
#   - 裸 wait 换成 wait -n 监督循环：任一关键进程退出即让容器重启，配合 unless-stopped 自愈
# =============================================================
set -e

PROFILES_DIR=${PROFILES_DIR:-/profiles}
# WINDOW_SIZE 格式: "WIDTH,HEIGHT"（用户友好），Xvfb 需要 "WxHxD"
WINDOW_SIZE=${WINDOW_SIZE:-1280,720}
WIDTH=$(echo $WINDOW_SIZE | cut -d, -f1)
HEIGHT=$(echo $WINDOW_SIZE | cut -d, -f2)
CHROME_FLAGS=${CHROME_FLAGS:-""}

echo "=========================================="
echo "  chrome-cdp 启动中..."
echo "=========================================="
echo "  Display:       :99"
echo "  noVNC:         http://localhost:6080"
echo "  VNC:           localhost:5900"
echo "  CDP:           http://localhost:9222"
echo "  Profiles:      $PROFILES_DIR"
echo "  Window size:   ${WIDTH}x${HEIGHT}"
echo "=========================================="

# ============================================
# 1. 清理残留锁
# ============================================
mkdir -p "$PROFILES_DIR"
rm -f /tmp/.X99-lock || true
rm -f "$PROFILES_DIR"/SingletonLock \
      "$PROFILES_DIR"/SingletonSocket \
      "$PROFILES_DIR"/SingletonCookie || true

# ============================================
# 2. 启动 Xvfb 虚拟显示
# ============================================
echo "[1/4] 启动 Xvfb 虚拟显示..."
Xvfb :99 -screen 0 ${WIDTH}x${HEIGHT}x24 -ac -nolisten tcp &
XVFB_PID=$!
sleep 2

if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb 启动失败"
    exit 1
fi

# ============================================
# 3. 启动 VNC 套件
# ============================================
echo "[2/4] 启动 x11vnc..."
# VNC 鉴权：设了 VNC_PASSWORD 就用 -rfbauth，否则保持 -nopw（仅当 host 端口只绑 127.0.0.1 时才可接受）
if [ -n "${VNC_PASSWORD:-}" ]; then
    mkdir -p /root/.vnc
    x11vnc -storepasswd "$VNC_PASSWORD" /root/.vnc/passwd >/dev/null 2>&1
    x11vnc -display :99 -forever -shared -rfbauth /root/.vnc/passwd -rfbport 5900 &
    echo "  VNC 鉴权:          ON (-rfbauth)"
else
    x11vnc -display :99 -forever -shared -nopw -rfbport 5900 &
    echo "  VNC 鉴权:          OFF (-nopw) —— 确认 6080 未暴露到公网/LAN"
fi
X11VNC_PID=$!
sleep 1
if ! kill -0 $X11VNC_PID 2>/dev/null; then
    echo "ERROR: x11vnc 启动失败"
    exit 1
fi

echo "[2/4] 启动 noVNC（Web VNC）..."
websockify --web=/usr/share/novnc 6080 localhost:5900 &
NOVNC_PID=$!
sleep 1
if ! kill -0 $NOVNC_PID 2>/dev/null; then
    echo "ERROR: websockify 启动失败"
    exit 1
fi

# ============================================

# ============================================

# 4. 启动 Chrome
# ============================================
echo "[3/4] 启动 Chrome（X11 模式）..."
# ============================================
# 3.5 启动 opencli bridge daemon（让扩展在 Chrome 启动时即连上，避免默认 disconnected）
# 注意：daemon 在下面 export HOME 之前启动；它依赖镜像 ENV / Compose 的 HOME，
# 不能只靠 entrypoint 里的 export，否则 daemon 会用 /root/.opencli。
# ============================================
echo "[3.5/4] 启动 opencli bridge daemon..."
opencli daemon restart > /var/log/opencli-daemon.log 2>&1 &
# 该命令是"启动器"：真正守护进程会重新挂到 PID1 下，启动器本身几秒内退出。
# disown 把它移出任务表，避免 wait -n 把它退出当成关键进程死亡而误重启。
disown
sleep 2


export DISPLAY=:99
# 仅影响 entrypoint 启动的子进程；docker exec 不继承这里，镜像 ENV 也设了 HOME
export HOME="$PROFILES_DIR"

CHROME_CMD="google-chrome \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-software-rasterizer \
    --window-size=${WIDTH},${HEIGHT} \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --remote-allow-origins=* \
    --user-data-dir=${PROFILES_DIR} \
    --no-first-run \
    --profile-directory=Default"

# 预装扩展（如果存在）
EXTENSIONS_DIR="/app/extensions"
if [ -d "$EXTENSIONS_DIR" ]; then
    for ext_dir in "$EXTENSIONS_DIR"/*/; do
        if [ -f "${ext_dir}manifest.json" ]; then
            CHROME_CMD="$CHROME_CMD --load-extension=${ext_dir}"
        fi
    done
fi

CHROME_CMD="$CHROME_CMD ${CHROME_FLAGS}"

echo "执行: $CHROME_CMD"
$CHROME_CMD about:blank > /var/log/chrome.log 2>&1 &
CHROME_PID=$!

# ============================================
# 5. 等待 Chrome 起来
# ============================================
echo "[4/4] 等待 Chrome 启动..."
sleep 3

# 验证 Chrome 是否在跑
if ! kill -0 $CHROME_PID 2>/dev/null; then
    echo "ERROR: Chrome 启动失败，查看日志："
    cat /var/log/chrome.log
    exit 1
fi

# 验证 CDP 9222 是否在听
CDP_READY=0
for i in 1 2 3 4 5; do
    if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        CDP_READY=1
        echo ""
        echo "=========================================="
        echo "  ✅ chrome-cdp 启动成功！"
        echo "=========================================="
        echo "  🌐 noVNC (Web VNC):  http://localhost:6080"
        echo "  🖥️  VNC 客户端:       localhost:5900"
        echo "  🔌 CDP (Agent 调用): http://localhost:9222"
        echo "  💻 opencli:           opencli --help"
        echo "=========================================="
        echo ""
        break
    fi
    echo "  等待 CDP 端口... (${i}/5)"
    sleep 2
done

# CDP 始终没起来：进程虽在但调试端口不可用，视为启动失败让容器重启，
# 否则会出现"进程在跑 / healthcheck 一直 unhealthy / unless-stopped 不重启"的死寂容器。
if [ "$CDP_READY" != "1" ]; then
    echo "ERROR: CDP 端口 9222 探测 5 次仍不可用，退出以便容器重启"
    echo "----- /var/log/chrome.log (tail) -----"
    tail -n 40 /var/log/chrome.log 2>/dev/null || true
    exit 1
fi

# ============================================
# 6. 优雅退出处理
# ============================================
cleanup() {
    [ -n "${WATCHDOG_PID:-}" ] && kill $WATCHDOG_PID 2>/dev/null || true
    [ -n "${MCP_PID:-}" ] && kill $MCP_PID 2>/dev/null || true
    echo "[$(date '+%F %T')] 收到退出信号，关闭进程..."
    kill $CHROME_PID   2>/dev/null || true
    kill $SOCAT_PID    2>/dev/null || true
    kill $NOVNC_PID    2>/dev/null || true
    kill $X11VNC_PID   2>/dev/null || true
    kill $XVFB_PID     2>/dev/null || true
    pkill -f 'opencli/dist/src/daemon' 2>/dev/null || true
}
# 信号退出：正常收尾并以 0 退出（不被 unless-stopped 重启）
trap 'cleanup; exit 0' SIGTERM SIGINT


# ============================================
# 6.5 启动 socat 转发 0.0.0.0:19999 -> 127.0.0.1:9222
#     让 services 网络内其他容器能直连 CDP
# ============================================
echo "  CDP socat:        0.0.0.0:19999 -> 127.0.0.1:9222"
socat TCP-LISTEN:19999,fork,reuseaddr TCP:127.0.0.1:9222 > /var/log/socat.log 2>&1 &
SOCAT_PID=$!
sleep 1
if ! kill -0 $SOCAT_PID 2>/dev/null; then
    echo "ERROR: socat 启动失败，查看 /var/log/socat.log："
    cat /var/log/socat.log 2>/dev/null || true
    exit 1
fi


echo "  socat PID:        $SOCAT_PID"


# ============================================
# 6.9 启动 opencli-mcp（把 opencli 的 1305 条命令暴露成 MCP 工具）
#     依赖已 vendored 在 /app/pylib（bind mount，重建不丢）
# ============================================
MCP_PID=""
if [ -f /app/mcp/opencli_mcp.py ] && [ -d /app/pylib/mcp ]; then
  echo "启动 opencli-mcp :8090 ..."
  PYTHONPATH=/app/pylib OPENCLI_MCP_PORT=8090 OPENCLI_MCP_TOKEN="${OPENCLI_MCP_TOKEN:-}" OPENCLI_MCP_AUDIT="${OPENCLI_MCP_AUDIT:-/var/log/opencli-mcp.log}" \
    python3 /app/mcp/opencli_mcp.py > /var/log/opencli-mcp.log 2>&1 &
  MCP_PID=$!
  sleep 2
  if kill -0 $MCP_PID 2>/dev/null; then
    echo "  opencli-mcp PID:  $MCP_PID"
  else
    echo "ERROR: opencli-mcp 启动失败，日志 /var/log/opencli-mcp.log："
    tail -20 /var/log/opencli-mcp.log 2>/dev/null || true
    MCP_PID=""
  fi
else
  echo "  opencli-mcp: 跳过（脚本或依赖缺失）"
fi

# ============================================
# 7.5 启动容器内 watch dog（L2 自愈：MCP 假死重启、daemon flaky 修复、残留标签页清理）
# ============================================
if [ -x /app/watchdog.sh ]; then
  mkdir -p /var/log/mcp
  OPENCLI_MCP_TOKEN="${OPENCLI_MCP_TOKEN:-}" OPENCLI_MCP_AUDIT="${OPENCLI_MCP_AUDIT:-/var/log/opencli-mcp.log}" \
    /app/watchdog.sh >> /var/log/mcp/watchdog.log 2>&1 &
  WATCHDOG_PID=$!
  echo "  watchdog PID:     $WATCHDOG_PID"
fi

# ============================================
# 7. 阻塞保持容器运行（监督模式）
#    - 关键基础设施（Xvfb/Chrome/x11vnc/websockify/socat）死亡 → 退出容器让 docker 重启
#    - MCP 死亡 → 在容器内重启（不重启容器），由 watchdog 也做兜底
# ============================================
echo "容器运行中，关键进程死亡将触发重启（Ctrl+C / docker stop 走优雅退出）..."
while true; do
  sleep 5
  dead=""
  kill -0 $XVFB_PID    2>/dev/null || dead="$dead Xvfb"
  kill -0 $X11VNC_PID  2>/dev/null || dead="$dead x11vnc"
  kill -0 $NOVNC_PID   2>/dev/null || dead="$dead websockify"
  kill -0 $CHROME_PID  2>/dev/null || dead="$dead Chrome"
  kill -0 $SOCAT_PID   2>/dev/null || dead="$dead socat"
  if [ -n "$dead" ]; then
    echo "[$(date '+%F %T')] 关键基础设施退出:${dead}，退出容器让 docker 重启"
    cleanup
    exit 1
  fi
  # MCP：死亡则在容器内重启
  if [ -n "${MCP_PID:-}" ] && ! kill -0 $MCP_PID 2>/dev/null; then
    echo "[$(date '+%F %T')] opencli-mcp 已退出，容器内重启..."
    if [ -f /app/mcp/opencli_mcp.py ] && [ -d /app/pylib/mcp ]; then
      PYTHONPATH=/app/pylib OPENCLI_MCP_PORT=8090 OPENCLI_MCP_TOKEN="${OPENCLI_MCP_TOKEN:-}" \
        OPENCLI_MCP_AUDIT="${OPENCLI_MCP_AUDIT:-/var/log/opencli-mcp.log}" \
        python3 /app/mcp/opencli_mcp.py >> /var/log/opencli-mcp.log 2>&1 &
      MCP_PID=$!
      echo "[$(date '+%F %T')] opencli-mcp 重启完成 (PID $MCP_PID)"
    fi
  fi
done
