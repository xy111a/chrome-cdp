# opencli-mcp

把 [opencli](../README.md) 的上千条站点命令包装成 **4 个 MCP 工具**，让任意 AI agent
通过 [Model Context Protocol](https://modelcontextprotocol.io) 驱动一台**已登录的真实 Chrome**。

> **为什么不是「一条命令一个工具」**
>
> opencli 实测 1305 条命令。全量映射成 MCP 工具，仅工具定义就约 **110,000 token** ——
> 占满 128k 上下文窗口的 86%，agent 既没空间干活，也在持续烧钱。本项目用**渐进式发现**：
> `list` 看有什么 → `help` 查参数 → `run` 执行，和人类敲 `--help` 一个逻辑。
> 功能一条不少，常驻开销约 700 token。

## 工具

| 工具 | 用途 |
|---|---|
| `opencli_guide()` | 用法手册（三段式流程、session 规则、写操作警告、常见坑） |
| `opencli_list(query, site, limit, offset)` | 发现命令，每条带 `access: read\|write` |
| `opencli_help(command)` | 单条命令的完整参数说明 |
| `opencli_run(command, ...)` | 执行任意命令（含写操作） |

## 返回结构

工具返回一个 JSON 字符串。解析一次即可拿到结构化的 `stdout`：

```json
{
  "ok": true,
  "error_code": null,
  "returncode": 0,
  "stdout": { "ok": true, "data": { "…": "…" } },
  "stderr": "",
  "truncated": false,
  "duration_ms": 5877,
  "command": "opencli zhihu hot --limit 3 -f json"
}
```

`stdout` 有两种形态，取决于子进程输出能否被解析：

- **能解析成 JSON** → 直接内联成对象（上面那样），调用方不再需要二次 `json.loads`；
- **不能解析**（普通文本，或被 `MAX_OUTPUT` 截断） → 保持原始字符串，同时 `truncated: true`。

> 改动记录：早期版本恒为字符串（`"stdout": "{\"ok\":true,…}"`），调用方要解析两次，
> 且错误信息也被包两层。现在的内联只发生在**序列化到工具出口**这一层，
> 内部拼接 `out + stderr` 仍按字符串处理，避免 `dict + str` 崩。

## 运行

```bash
# 容器内（由 entrypoint 启动，见上一级 Dockerfile）
PYTHONPATH=/app/pylib OPENCLI_MCP_PORT=8090 OPENCLI_MCP_TOKEN="$TOKEN" python3 /app/mcp/opencli_mcp.py
```

| 环境变量 | 说明 |
|---|---|
| `OPENCLI_MCP_PORT` | 监听端口，默认 8090 |
| `OPENCLI_MCP_TOKEN` | Bearer token；**留空则不鉴权**。`/health` 始终免鉴权 |
| `OPENCLI_MCP_AUDIT` | 审计日志路径（每次工具调用的工具名、参数摘要） |
| `OPENCLI_MCP_SERIALIZE` | 保持 `1`：串行执行，避免并发抢同一个 Chrome 标签页 |

客户端配置（Hermes / Claude Desktop 等）：

```yaml
mcp_servers:
  opencli:
    url: http://<host>:8090/mcp
    headers:
      Authorization: "Bearer ${OPENCLI_MCP_TOKEN}"
    enabled: true
```

## 关键约定

- **`command` 参数不要带前导 `opencli`**：写 `list -f json --limit 5`，不是 `opencli list …`。
- **`/health` 免鉴权**，用于容器 healthcheck 与看门狗；`/mcp` 需 token，未带 → 401。
- **并发**：默认串行（`OPENCLI_MCP_SERIALIZE=1`）。Chrome 的同一个标签页被两个命令同时驱动会互相打断，
  所以不要为了吞吐关掉它 —— 要并行就起第二个 chrome 容器。
- **写操作**：`opencli_list` 会给每条命令标注 `access: read|write`；`unknown` 一律当 write 对待。
- **token 轮换**：Chrome 容器重启后客户端仍持有旧 MCP session（`MCPError: Session not found`），
  工具会从会话里消失，直到重启 agent 侧 gateway。兜底做法见 `opencli_mcp_watchdog.sh` 同级的看门狗脚本思路：
  `/health` 或 `initialize` 不通就重启对应 profile 的 gateway（401 只告警，重启无效）。

## 部署形态（本仓库采用）

镜像内 `/app/mcp` 是烘焙进去的。为了方便改代码不重建镜像，推荐给它加一个只读 bind mount：

```yaml
volumes:
  - ./mcp:/app/mcp:ro
```

改完 `opencli_mcp.py` 后重启容器即可生效；没有这个挂载，改容器内文件会在下次 `--force-recreate` 时丢失
（这是踩过的坑）。
