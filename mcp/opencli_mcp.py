#!/usr/bin/env python3
"""
opencli-mcp —— 把 opencli 的上千条命令暴露成 4 个 MCP 工具。

为什么要「发现 + 执行」，而不是一条命令一个工具：
1305 条命令全量映射成 MCP 工具，工具定义约 110,000 token —— 占满 128k 上下文
的 86%，agent 既没空间干活也在持续烧钱。这里用渐进式发现：先 list 看有什么，
再 run 执行，和人类用 CLI 的 --help 习惯一致。功能一条不少。

工具：
  opencli_guide()                   用法手册
  opencli_list(query, site, ...)    发现命令（含 read/write 标记与描述）
  opencli_help(command)             单条命令的完整参数说明
  opencli_run(command, args, ...)   执行任意命令（全量，含写操作）

鉴权：设置 OPENCLI_MCP_TOKEN 后，所有请求必须带 `Authorization: Bearer <token>`；
      /health 免鉴权，其余一律 401。不设置则不做校验（仅建议在内网使用）。
"""

from __future__ import annotations

import hmac
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime

from mcp.server.fastmcp import FastMCP

VERSION = "1.2.0"

OPENCLI_BIN = os.environ.get("OPENCLI_BIN", "opencli")
PORT = int(os.environ.get("OPENCLI_MCP_PORT", "8090"))
HOST = os.environ.get("OPENCLI_MCP_HOST", "0.0.0.0").strip() or "0.0.0.0"  # nosec B104
TOKEN = os.environ.get("OPENCLI_MCP_TOKEN", "").strip()
AUDIT_LOG = os.environ.get("OPENCLI_MCP_AUDIT", "/var/log/opencli-mcp.log")
AUDIT_MAX_BYTES = int(os.environ.get("OPENCLI_MCP_AUDIT_MAX_BYTES", str(5 * 1024 * 1024)))
MAX_OUTPUT = int(os.environ.get("OPENCLI_MCP_MAX_OUTPUT", "30000"))
DEFAULT_TIMEOUT = int(os.environ.get("OPENCLI_MCP_TIMEOUT", "120"))
CONCURRENCY = int(os.environ.get("OPENCLI_MCP_CONCURRENCY", "1"))
# 向后兼容：老的 SERIALIZE=0 等价于并发 8
if os.environ.get("OPENCLI_MCP_SERIALIZE") == "0" and "OPENCLI_MCP_CONCURRENCY" not in os.environ:
    CONCURRENCY = 8
if CONCURRENCY < 1:
    CONCURRENCY = 1
SERIALIZE = CONCURRENCY <= 1
ALLOW_NO_AUTH = os.environ.get("OPENCLI_MCP_ALLOW_NO_AUTH", "").strip() in ("1", "true", "yes")

# 同一台 Chrome、同一份登录态：并发操作会互相抢标签页和 cookie。
# 通过 OPENCLI_MCP_CONCURRENCY 控制（默认 1 = 串行）。
_EXEC_SEM = threading.Semaphore(CONCURRENCY)
_EXEC_LOCK = threading.Lock() if SERIALIZE else None

TOOLS = ["opencli_guide", "opencli_list", "opencli_help", "opencli_run", "opencli_auth_status"]

mcp = FastMCP("opencli", host=HOST, port=PORT)

GUIDE = """# opencli-mcp 使用说明

opencli 是「把任意网站变成 CLI」的工具，上千条命令覆盖近两百个站点，运行在
真实 Chrome 上、**带登录态**。它不是爬虫，而是以「你」的身份操作网页。

## 三段式用法（不要跳过前两步）

1. `opencli_list(query=..., site=...)` —— 先查有哪些命令。**不要凭猜写命令名**，
   命令命名不统一（`zhihu hot`、`twitter search`、`browser screenshot`）。
2. `opencli_help(command="...")` —— 查这条命令的参数、选项、必填项。
3. `opencli_run(command="...", args=[...])` —— 执行。

不确定站点名时先 `opencli_list(limit=100)` 看全貌，用返回的 `nextOffset` 翻页继续。

## 返回格式

五个工具都返回 JSON 字符串：
`{ok, error_code, returncode, stdout, stderr, duration_ms, command, truncated}`
- `ok=false` 时先看 `stdout` 里的结构化 error，再看 `stderr` 和 `returncode`。
- `stdout` 超过 30000 字符会被截断（`truncated=true`），此时用 `--limit`、
  或用 `args` 传更精确的参数缩小结果，不要去猜被截掉的内容。

## browser 系列

`opencli_run(command="browser click", args=["--session", "main", "#id"])`

- **页面类命令必须带 `--session <名字>`**，会话不存在会报错——先用
  `opencli_run(command="browser open", args=["--session","main","https://..."])` 建立。
- 可用动作：open / click / dblclick / hover / fill / type / keys / scroll / select /
  upload / screenshot / eval / extract / find / wait / frames / network / console /
  dialog / tab / state / back / get。
- `browser screenshot` 不带路径时**直接返回 base64**，带路径则写文件（写文件后你
  读不到，除非另有取文件通道——优先不要带路径）。
- 需要原生窗口/标签管理时用 `browser chrome describe|call`，不要开新标签页。
- 用完会话记得 `browser close --session <名字>` 释放。

## ⚠️ 写操作

`access: write` 的命令会**以你的账号对外产生真实行为**：发帖、评论、关注、收藏、
给 BOSS 直聘 HR 打招呼、下单。

执行前必须：
1. 确认这是用户明确要的（不是"顺手试试"）；
2. 把将要发生的动作原样复述给用户（发什么、发给谁、在哪个站）；
3. 拿到确认后再 `opencli_run`。

`opencli_list` 返回的每条命令都带 `access` 字段，先看它是 read 还是 write。
上游少数命令没有标注时，本服务补为 `unknown` —— **`unknown` 一律按 write 对待**。

## 常见坑

- `opencli_list` 默认只返回 25 条（本服务默认 40），**不要以为这就是全部**；用
  `limit` + `offset` 翻完（`limit` 上限 100）。
- 命令慢是正常的：真实浏览器操作，5–15 秒常见。`timeout_s` 默认 120，别设太小。
- 本服务**默认串行执行**：同一时刻只有一条命令在跑。并发调用会排队而不是打架，
  所以看到耗时变长可能是前面有人排着，不是卡死。
- 有些命令依赖登录，未登录站点会返回空结果或登录提示，不是工具坏了。
- 输出用 `format="json"`（默认）最好解析；`table`/`yaml` 只适合人眼看。
- **少数子命令不支持 `-f`**（例如 `browser chrome describe`）。本服务会自动去掉
  `-f` 重试一次并在结果里加 `note` 字段，看到 `note` 说明是这种情况，不是错误。
"""


def _audit(entry: dict) -> None:
    """追加一行 JSON 审计。失败绝不影响主流程；超过阈值自动轮转一份。"""
    try:
        if os.path.exists(AUDIT_LOG) and os.path.getsize(AUDIT_LOG) > AUDIT_MAX_BYTES:
            os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _maybe_json(value):
    """stdout 往往本身是 JSON 字符串：能解析就直接内联成对象，调用方只解析一次。"""
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def _dump_result(res):
    """对外序列化：stdout 若是 JSON 字符串，内联成对象，调用方只解析一次。"""
    if isinstance(res, dict):
        res = dict(res)
        res["stdout"] = _maybe_json(res.get("stdout"))
    return json.dumps(res, ensure_ascii=False)


def _run(argv: list[str], timeout: int) -> dict:
    started = time.time()

    def _invoke() -> dict:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return {"rc": r.returncode, "out": r.stdout, "err": r.stderr}
        except subprocess.TimeoutExpired:
            return {"error": f"timeout after {timeout}s", "error_code": "timeout"}
        except FileNotFoundError:
            return {"error": f"{argv[0]} binary not found", "error_code": "not_found"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "error_code": "exec_failed"}

    with _EXEC_SEM:
        got = _invoke()

    elapsed = int((time.time() - started) * 1000)
    if "error" in got:
        return {
            "ok": False,
            "error": got["error"],
            "error_code": got["error_code"],
            "command": " ".join(argv),
            "duration_ms": elapsed,
        }

    out, truncated = got["out"], False
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n... [truncated {len(got['out']) - MAX_OUTPUT} chars]"
        truncated = True
    combined = out + (got["err"] or "")
    fmt_unsupported = "unknown option '-f'" in combined or "unknown option '--format'" in combined
    return {
        "ok": got["rc"] == 0,
        "error_code": None
        if got["rc"] == 0
        else ("format_unsupported" if fmt_unsupported else "exec_failed"),
        "returncode": got["rc"],
        "stdout": out,
        "stderr": (got["err"] or "")[:4000],
        "truncated": truncated,
        "duration_ms": elapsed,
        "command": " ".join(argv),
    }


@mcp.tool()
def opencli_guide() -> str:
    """Read this FIRST once per session: how to use the other three tools well.

    Returns the usage playbook (discovery pattern, session rules, write-op warnings,
    known pitfalls). Cheap to call, do it before your first opencli_run.
    """
    _audit({"tool": "opencli_guide"})
    return GUIDE


@mcp.tool()
def opencli_list(query: str = "", site: str = "", limit: int = 40, offset: int = 0) -> str:
    """Discover available commands. Call this BEFORE opencli_run when unsure.

    Args:
        query: filter by command name or description, e.g. "zhihu" or "search".
        site: restrict to one adapter/site, e.g. "twitter", "browser", "zhihu".
        limit: max results (1-100).
        offset: pagination offset (use data.nextOffset from the previous reply).
    """
    argv = [OPENCLI_BIN, "list", "-f", "json", "--limit", str(max(1, min(100, limit)))]
    if query:
        argv.append(query)
    if site:
        argv += ["--site", site]
    if offset:
        argv += ["--offset", str(max(0, offset))]
    res = _run(argv, 60)
    # 上游部分命令不带 access 字段（如 browser frames）。缺省按 unknown 补，
    # 让调用方永远能拿到这个字段；unknown 应视为 write 对待。
    if res.get("ok") and res.get("stdout"):
        try:
            raw = json.loads(res["stdout"])
            cmds = (raw.get("data") or {}).get("commands")
            if isinstance(cmds, list):
                missing = 0
                for c in cmds:
                    if isinstance(c, dict) and not c.get("access"):
                        c["access"] = "unknown"
                        missing += 1
                if missing:
                    res["stdout"] = json.dumps(raw, ensure_ascii=False)
                    res["note"] = f"{missing} 条命令上游未标 access，已补为 unknown（按 write 对待）"
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    _audit({"tool": "opencli_list", "query": query, "site": site, "ok": res.get("ok")})
    return _dump_result(res)


@mcp.tool()
def opencli_help(command: str) -> str:
    """Show the full parameter reference for one command, e.g. "zhihu hot" or "browser click".

    Returns the CLI's own --help text (arguments, options, defaults, choices).
    """
    parts = shlex.split(command or "")
    if not parts:
        return json.dumps({"ok": False, "error": "empty command"}, ensure_ascii=False)
    res = _run([OPENCLI_BIN] + parts + ["--help"], 60)
    _audit({"tool": "opencli_help", "command": command, "ok": res.get("ok")})
    return _dump_result(res)


@mcp.tool()
def opencli_run(
    command: str, args: list[str] | None = None, format: str = "json", timeout_s: int = DEFAULT_TIMEOUT
) -> str:
    """Execute any opencli command. Full surface: read AND write (post, follow, comment...).

    Args:
        command: command path without the leading "opencli", e.g. "zhihu hot",
                 "twitter search", "browser screenshot".
        args: extra argv passed verbatim, e.g. ["--limit", "10"] or ["AI news"].
        format: output format; json (default) is best for you to parse.
        timeout_s: seconds before the call is killed (max 300).
    """
    parts = shlex.split(command or "")
    if not parts:
        return json.dumps({"ok": False, "error": "empty command"}, ensure_ascii=False)

    extra = list(args or [])
    argv = [OPENCLI_BIN] + parts + extra
    fmt = (format or "").strip()
    if fmt and "-f" not in argv and "--format" not in argv:
        argv += ["-f", fmt]
    timeout = max(5, min(300, int(timeout_s)))
    res = _run(argv, timeout)

    # 部分子命令不接受 -f/--format（如 browser chrome describe），自动去掉重试一次
    combined = (res.get("stdout") or "") + (res.get("stderr") or "")
    if not res.get("ok") and ("unknown option '-f'" in combined or "unknown option '--format'" in combined):
        res = _run([OPENCLI_BIN] + parts + extra, timeout)
        res["note"] = "该命令不支持 -f/--format，已去掉格式化参数重试"

    _audit(
        {
            "tool": "opencli_run",
            "command": command,
            "args": extra,
            "ok": res.get("ok"),
            "returncode": res.get("returncode"),
            "duration_ms": res.get("duration_ms"),
        }
    )
    return _dump_result(res)


@mcp.tool()
def opencli_auth_status() -> str:
    """Check which sites opencli is currently logged in to (read-only).

    Returns the CLI's auth status output. Run this before any write operation to
    confirm the target site is authenticated; unauthenticated sites return empty
    results or login prompts, not tool errors.
    """
    res = _run([OPENCLI_BIN, "auth", "status"], 60)
    _audit({"tool": "opencli_auth_status", "ok": res.get("ok")})
    return _dump_result(res)


# MCP Resource: 暴露 guide 为可缓存资源，和 tool 形成互补
@mcp.resource("opencli://guide")
def guide_resource() -> str:
    return GUIDE


# MCP Prompt: 一键进入只读调查模式
@mcp.prompt()
def readonly_investigation(site: str = "") -> str:
    """Guide the agent to investigate a site read-only (no writes)."""
    scope = (" on " + site) if site else ""
    return (
        f"You are investigating{scope} using opencli. Rules: only use commands whose "
        "access is 'read' or 'public'; never run 'write'/'unknown' commands; "
        "confirm with opencli_list first; report findings with sources."
    )


# 启动时探测 opencli 可用性 + 版本
_OPENCLI_VERSION = None
try:
    _v = subprocess.run([OPENCLI_BIN, "--version"], capture_output=True, text=True, timeout=10)
    _OPENCLI_VERSION = (_v.stdout or _v.stderr).strip() or None
except Exception:  # noqa: BLE001
    _OPENCLI_VERSION = None

# ---------- 健康探针（带缓存，避免频繁 subprocess）----------
_DOCTOR_CACHE = {"ts": 0, "data": None}
_DOCTOR_TTL = int(os.environ.get("OPENCLI_MCP_DOCTOR_TTL", "60"))


def _doctor(force: bool = False) -> dict:
    """运行 opencli doctor，返回结构化状态；TTL 内复用缓存。"""
    now = time.time()
    if not force and _DOCTOR_CACHE["data"] is not None and (now - _DOCTOR_CACHE["ts"]) < _DOCTOR_TTL:
        return _DOCTOR_CACHE["data"]
    try:
        r = subprocess.run([OPENCLI_BIN, "doctor"], capture_output=True, text=True, timeout=15)
        raw = (r.stdout or "") + (r.stderr or "")
        status = {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "raw": raw.strip()[:2000],
            "daemon_running": "daemonRunning" in raw and '"daemonRunning": true' in raw.replace(" ", ""),
            "daemon_flaky": '"daemonFlaky": true' in raw.replace(" ", ""),
            "daemon_stale": '"daemonStale": true' in raw.replace(" ", ""),
            "extension_connected": '"extensionConnected": true' in raw.replace(" ", ""),
        }
        # 容错：用 json 解析更可靠
        try:
            # doctor 输出可能混入非 json 文本，取第一个 {...}
            import re as _re
            m = _re.search(r"\{.*\}", raw, _re.S)
            if m:
                d = json.loads(m.group(0))
                if isinstance(d.get("data"), dict):
                    d = d["data"]
                status["daemon_running"] = bool(d.get("daemonRunning"))
                status["daemon_flaky"] = bool(d.get("daemonFlaky"))
                status["daemon_stale"] = bool(d.get("daemonStale"))
                status["extension_connected"] = bool(d.get("extensionConnected"))
                status["cli_version"] = d.get("cliVersion")
                status["extension_version"] = d.get("extensionVersion")
                status["extension_outdated"] = bool(d.get("extensionOutdated"))
        except Exception:  # noqa: BLE001
            pass
        _DOCTOR_CACHE["ts"] = now
        _DOCTOR_CACHE["data"] = status
        return status
    except Exception as e:  # noqa: BLE001
        status = {"ok": False, "error": f"{type(e).__name__}: {e}", "daemon_running": False,
                  "daemon_flaky": False, "daemon_stale": False, "extension_connected": False}
        _DOCTOR_CACHE["ts"] = now
        _DOCTOR_CACHE["data"] = status
        return status


def _deep_health() -> dict:
    """深度健康：MCP + opencli doctor 综合。返回 (payload, http_status)。"""
    doc = _doctor()
    healthy = bool(doc.get("daemon_running") and doc.get("extension_connected"))
    # daemon_flaky/stale 降级为 degraded 但不算 down
    degraded = bool(doc.get("daemon_flaky") or doc.get("daemon_stale") or doc.get("extension_outdated"))
    return {
        "ok": healthy,
        "service": "opencli-mcp",
        "version": VERSION,
        "auth": bool(TOKEN),
        "serialized": SERIALIZE,
        "concurrency": CONCURRENCY,
        "host": HOST,
        "bin": OPENCLI_BIN,
        "opencli_version": _OPENCLI_VERSION,
        "tools": TOOLS,
        "components": {
            "mcp": "ok",
            "opencli_daemon": "ok" if doc.get("daemon_running") else "down",
            "extension": "ok" if doc.get("extension_connected") else "down",
            "daemon_flaky": doc.get("daemon_flaky"),
            "daemon_stale": doc.get("daemon_stale"),
            "extension_outdated": doc.get("extension_outdated"),
            "extension_version": doc.get("extension_version"),
        },
        "status": "healthy" if healthy and not degraded else ("degraded" if healthy else "unhealthy"),
        "doctor": doc,
    }, (200 if healthy else 503)


def build_app():
    """统一构建 ASGI app：有 token 就挂 bearer 中间件，/health 始终免鉴权。"""
    import uvicorn  # noqa: F401  (仅用于可用性检查)
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    app = mcp.streamable_http_app()

    if TOKEN:
        expected = ("Bearer " + TOKEN).encode()

        class Bearer(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path in ("/health", "/healthz", "/ready"):
                    return await call_next(request)
                got = request.headers.get("authorization", "").encode()
                if not hmac.compare_digest(got, expected):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(Bearer)

    async def _health(_request):
        return JSONResponse(
            {
                "ok": True,
                "service": "opencli-mcp",
                "version": VERSION,
                "auth": bool(TOKEN),
                "serialized": SERIALIZE,
                "concurrency": CONCURRENCY,
                "host": HOST,
                "bin": OPENCLI_BIN,
                "opencli_version": _OPENCLI_VERSION,
                "tools": TOOLS,
            }
        )

    async def _ready(_request):
        payload, status = _deep_health()
        return JSONResponse(payload, status_code=status)

    app.router.routes.append(Route("/health", _health))
    app.router.routes.append(Route("/healthz", _health))
    app.router.routes.append(Route("/ready", _ready))
    return app


def main() -> None:
    if not TOKEN and not ALLOW_NO_AUTH:
        print(
            "[opencli-mcp] 拒绝启动：未设置 OPENCLI_MCP_TOKEN。\n"
            "  这个服务能驱动已登录的浏览器，裸奔等于把账号交出去。\n"
            "  确实要在内网无鉴权运行，请显式设置 OPENCLI_MCP_ALLOW_NO_AUTH=1。",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    if not TOKEN:
        print("[opencli-mcp] ⚠️  无鉴权模式（OPENCLI_MCP_ALLOW_NO_AUTH=1）", file=sys.stderr)

    # 启动自检：opencli 必须可用
    try:
        _chk = subprocess.run([OPENCLI_BIN, "--version"], capture_output=True, text=True, timeout=10)
        if _chk.returncode != 0:
            print(
                f"[opencli-mcp] ⚠️  opencli --version 返回 {_chk.returncode}: "
                f"{((_chk.stderr or '').strip()[:200])}",
                file=sys.stderr,
            )
    except FileNotFoundError:
        print(
            f"[opencli-mcp] ❌ 找不到 opencli 二进制：{OPENCLI_BIN} — 服务仍启动，但所有调用会失败",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[opencli-mcp] ⚠️  opencli 自检异常：{e}", file=sys.stderr)

    try:
        app = build_app()
    except ImportError as e:
        print(f"缺少 uvicorn/starlette，无法启动 HTTP 服务: {e}", file=sys.stderr)
        raise SystemExit(1) from None

    import uvicorn

    print(
        f"[opencli-mcp] v{VERSION} listening on {HOST}:{PORT} "
        f"(auth {'ON' if TOKEN else 'OFF'}, concurrency {CONCURRENCY}"
        f"{', serialize' if SERIALIZE else ''})",
        flush=True,
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
