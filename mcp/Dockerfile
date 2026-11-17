# opencli-mcp — 把 opencli 的命令面包装成 4 个 MCP 工具
#
# 这是 chrome-cdp 基座镜像之上的「烘焙层」：基座提供 Chrome + CDP + noVNC + opencli CLI，
# 这一层加上 opencli-mcp（HTTP MCP 服务，默认 :8090）以及配套的 entrypoint / 健康检查 / 看门狗。
#
# 构建：
#   docker build -t chrome-opencli:1.4.0 .
#   （可用 --build-arg BASE_IMAGE=... / OPENCLI_VERSION=... 覆写钉住的版本）
#
# 依赖不随仓库分发：Python 依赖在构建时用 pip 装进系统 site-packages。
# 早期做法是把 pylib/（43 MB 的 vendored 依赖树）一起 COPY 进来，仓库会迅速膨胀且难以审计，
# 这里改为 requirements.txt 钉版本 —— 需要离线构建时再自行 vendoring。

ARG BASE_IMAGE=ghcr.io/xy111a/chrome-opencli:1.3.0
FROM ${BASE_IMAGE}

# 基座镜像自带 opencli 2.0.2，这里升级到钉住的版本，避免构建时漂移
ARG OPENCLI_VERSION=2.0.3
RUN npm install -g @jackwener/opencli@${OPENCLI_VERSION}

# opencli-mcp 的 Python 依赖（钉版本）
#
# 装到 /app/pylib 而不是系统 site-packages：基座镜像本来就把依赖 vendored 在 /app/pylib，
# 并通过 PYTHONPATH 暴露。往系统装会让 pip 认为那些「装过但缺 RECORD」的包需要卸载，
# 直接失败（uninstall-no-record-file）。--target 只写文件，不触发卸载。
COPY requirements.txt /tmp/mcp-requirements.txt
RUN pip install --no-cache-dir --break-system-packages --upgrade \
        --target /app/pylib -r /tmp/mcp-requirements.txt \
    && rm -f /tmp/mcp-requirements.txt

# MCP 服务本体 + 运行时脚本
COPY opencli_mcp.py /app/mcp/opencli_mcp.py
COPY entrypoint.sh /app/entrypoint.sh
COPY healthcheck.sh /app/healthcheck.sh
COPY watchdog.sh /app/watchdog.sh

# 企业策略：强制安装并固定 OpenCLI Chrome 扩展（扩展由 entrypoint 按 pinned 版本拉取）
COPY policies/opencli-extension.json /etc/opt/chrome/policies/managed/opencli-extension.json

RUN chmod +x /app/entrypoint.sh /app/healthcheck.sh /app/watchdog.sh

# 8090 = opencli-mcp；19999/9222 = CDP；6080 = noVNC（由基座 entrypoint 启动）
EXPOSE 8090

ENTRYPOINT ["/app/entrypoint.sh"]
