#!/bin/bash
# 容器内自愈 watch dog（L2）
# 每 60s 一次：MCP 存活探针、opencli daemon 状态、残留标签页清理
set -u
LOG=/var/log/mcp/watchdog.log
mkdir -p /var/log/mcp
echo "[$(date '+%F %T')] watchdog 启动" >> "$LOG"

MCP_FAIL=0
EXT_FAIL=0
while true; do
  sleep 60

  # 1) MCP 存活探针（真 HTTP 响应）
  if ! curl -sf --max-time 8 http://localhost:8090/health >/dev/null 2>&1; then
    MCP_FAIL=$((MCP_FAIL+1))
    echo "[$(date '+%F %T')] MCP /health 失败 (${MCP_FAIL}/3)" >> "$LOG"
    if [ "$MCP_FAIL" -ge 3 ]; then
      echo "[$(date '+%F %T')] MCP 连续 3 次无响应，重启 MCP 进程" >> "$LOG"
      pkill -f 'opencli_mcp.py' 2>/dev/null || true
      sleep 2
      if [ -f /app/mcp/opencli_mcp.py ] && [ -d /app/pylib/mcp ]; then
        PYTHONPATH=/app/pylib OPENCLI_MCP_PORT=8090 OPENCLI_MCP_TOKEN="${OPENCLI_MCP_TOKEN:-}" \
          OPENCLI_MCP_AUDIT="${OPENCLI_MCP_AUDIT:-/var/log/opencli-mcp.log}" \
          python3 /app/mcp/opencli_mcp.py >> /var/log/opencli-mcp.log 2>&1 &
        echo "[$(date '+%F %T')] MCP 已重启 (PID $!)" >> "$LOG"
      fi
      MCP_FAIL=0
    fi
  else
    MCP_FAIL=0
  fi

  # 2) opencli doctor：daemon 状态 + 扩展连接
  DOCTOR=$(opencli doctor 2>/dev/null || true)
  if echo "$DOCTOR" | grep -q '"daemonFlaky": true'; then
    echo "[$(date '+%F %T')] daemon flaky，重启 daemon" >> "$LOG"
    opencli daemon restart >/dev/null 2>&1 || true
    sleep 2
  fi
  if echo "$DOCTOR" | grep -q '"daemonStale": true'; then
    echo "[$(date '+%F %T')] daemon stale，重启 daemon" >> "$LOG"
    opencli daemon restart >/dev/null 2>&1 || true
    sleep 2
  fi
  if echo "$DOCTOR" | grep -q '"extensionConnected": false'; then
    EXT_FAIL=$((EXT_FAIL+1))
    echo "[$(date '+%F %T')] 扩展未连接 (${EXT_FAIL}/10)" >> "$LOG"
    if [ "$EXT_FAIL" -ge 10 ]; then
      echo "[$(date '+%F %T')] 扩展连续 10 分钟未连接，重启容器" >> "$LOG"
      # 优雅退出让 docker 重启
      kill 1
    fi
  else
    EXT_FAIL=0
  fi

  # 3) 残留标签页清理：通过 CDP 计数，超过 15 个时清理 about:blank
  PAGES=$(curl -sf --max-time 5 http://localhost:9222/json/list 2>/dev/null | grep -c '"type": "page"' || echo 0)
  if [ "$PAGES" -gt 15 ]; then
    echo "[$(date '+%F %T')] 标签页 ${PAGES} 个，超阈值，清理 about:blank" >> "$LOG"
    # 用 opencli browser close 关闭空标签（best-effort）
    for sid in $(curl -sf --max-time 5 http://localhost:9222/json/list 2>/dev/null | grep -o '"id": "[^"]*"' | head -5 | sed 's/"id": "//;s/"//'); do
      curl -sf --max-time 3 "http://localhost:9222/json/close/$sid" >/dev/null 2>&1 || true
    done
  fi
done
