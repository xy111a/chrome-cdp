#!/bin/sh
# L1 容器健康探针：CDP + MCP 双探针（任一失败 → unhealthy）
set -e
curl -sf --max-time 5 http://localhost:19999/json/version >/dev/null || exit 1
curl -sf --max-time 8 http://localhost:8090/ready >/dev/null || exit 1
exit 0
