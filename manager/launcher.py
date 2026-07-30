"""
NanoClaw Manager Launcher

FastAPI 服务，提供：
1. 托管管理 UI（静态文件）
2. Config 直接读写（不依赖 NanoClaw Gateway 是否运行）
3. Gateway 进程管理（启动/停止/状态/日志推送）
4. MCP Server 配置管理（CRUD + 开关）
5. 工具端点（打开工作区、打开 CLI 终端）
"""

import json
import os
import subprocess
import sys
import webbrowser
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gateway_manager import get_gateway

# ── 路径常量 ──────────────────────────────────────────────
MANAGER_DIR = Path(__file__).parent
NANOCLAW_DIR = MANAGER_DIR.parent
CONFIG_PATH = NANOCLAW_DIR / "config.json"
UI_DIR = MANAGER_DIR / "ui"
LAUNCHER_PORT = 3000
LAUNCHER_CONFIG_PATH = MANAGER_DIR / "launcher_config.json"

PROJECT_MCP_SERVERS = {
    "foreign_trade_inquiry": {
        "module": "mcp_servers.foreign_trade_inquiry_server",
        "path": NANOCLAW_DIR / "mcp_servers" / "foreign_trade_inquiry_server.py",
        "description": "Foreign trade inquiry and quotation business server",
    },
    "poetry": {
        "module": "mcp_servers.poetry_server",
        "path": NANOCLAW_DIR / "mcp_servers" / "poetry_server.py",
        "description": "Poetry example MCP server",
    },
    "trade_rag": {
        "module": "trade_rag.server",
        "path": NANOCLAW_DIR / "trade_rag" / "server.py",
        "description": "Enterprise knowledge-base RAG MCP server",
    },
}


def _load_project_env() -> None:
    """让 Manager 与 Gateway 使用相同的项目根 .env；不覆盖进程环境。"""
    env_path = NANOCLAW_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        original_keys = set(os.environ)
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() and key.strip() not in original_keys:
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass


_load_project_env()


def _discover_project_mcp_servers() -> dict[str, dict]:
    """通过明确入口清单发现项目 MCP，不导入模块、不触发服务副作用。"""
    return {
        name: {
            "command": "{python}",
            "args": ["-m", item["module"]],
            "description": item["description"],
            "entry_exists": item["path"].is_file(),
        }
        for name, item in PROJECT_MCP_SERVERS.items()
        if item["path"].is_file()
    }

# ── Launcher 配置读写 ────────────────────────────────────


def _read_launcher_config() -> dict:
    """读取 launcher 自己的配置（python 路径等），不存在时返回默认。"""
    defaults = {"python_path": "", "_version": 1}
    if not LAUNCHER_CONFIG_PATH.exists():
        return defaults
    try:
        data = json.loads(LAUNCHER_CONFIG_PATH.read_text(encoding="utf-8"))
        return {**defaults, **data}
    except (json.JSONDecodeError, OSError):
        return defaults


def _write_launcher_config(data: dict) -> None:
    """写入 launcher 配置。"""
    data["_version"] = 1
    LAUNCHER_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

# ── Config 读写辅助 ──────────────────────────────────────


def _read_config() -> dict:
    """读取 config.json，不存在时返回空字典。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(data: dict) -> None:
    """写入 config.json，自动备份旧文件。"""
    # 备份
    if CONFIG_PATH.exists():
        bak = CONFIG_PATH.with_suffix(".json.manager_bak")
        try:
            bak.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")


def _mask_key(key: str) -> str:
    """掩码 API Key。"""
    if not key or len(key) < 8:
        return key
    return key[:4] + "****" + key[-4:]


# ── Pydantic 模型 ───────────────────────────────────────


class ConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    models: Optional[dict] = None
    max_iterations: Optional[int] = None
    workspace: Optional[str] = None


class McpServerConfig(BaseModel):
    command: str = Field(..., min_length=1)
    args: list[str] = Field(default_factory=list)
    description: Optional[str] = ""
    enabled: bool = True
    env: Optional[dict[str, str]] = None


class McpServerCreate(BaseModel):
    name: str = Field(..., min_length=1)
    config: McpServerConfig


class McpServerUpdate(BaseModel):
    config: McpServerConfig


# ── FastAPI 应用 ─────────────────────────────────────────

gateway = get_gateway()
ws_clients: list[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：后台任务推送日志到所有 WebSocket 客户端
    async def _push_logs():
        last_count = 0
        while True:
            import asyncio
            await asyncio.sleep(0.5)
            status = gateway.get_status()
            logs = status.get("logs", [])
            if len(logs) != last_count:
                new_lines = logs[last_count:]
                last_count = len(logs)
                stale = []
                for ws in ws_clients:
                    try:
                        for line in new_lines:
                            await ws.send_text(line)
                    except Exception:
                        stale.append(ws)
                for ws in stale:
                    if ws in ws_clients:
                        ws_clients.remove(ws)

    import asyncio
    task = asyncio.create_task(_push_logs())
    yield
    task.cancel()


app = FastAPI(title="NanoClaw Manager", lifespan=lifespan)

# CORS — 允许本地开发调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 托管静态 UI
app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


# ── 设置页面端点 ─────────────────────────────────────────


@app.get("/api/config")
async def get_config():
    """获取当前配置（API Key 掩码后返回）。"""
    cfg = _read_config()
    # 兼容 model 字段和 models.main 两种写法
    model = cfg.get("model", "")
    if not model and cfg.get("models"):
        model = cfg.get("models", {}).get("main", "")
    masked = {
        "api_key": _mask_key(cfg.get("api_key", "")),
        "api_key_raw": cfg.get("api_key", ""),
        "base_url": cfg.get("base_url", ""),
        "model": model,
        "models": cfg.get("models", None),
        "max_iterations": cfg.get("max_iterations", 32),
        "workspace": cfg.get("workspace", "."),
    }
    return masked


@app.put("/api/config")
async def update_config(update: ConfigUpdate):
    """更新配置字段。"""
    cfg = _read_config()
    update_data = update.model_dump(exclude_none=True)
    for key, value in update_data.items():
        cfg[key] = value
    _write_config(cfg)
    return {"status": "success", "message": "配置已更新"}


@app.put("/api/config/apikey")
async def update_apikey(body: dict):
    """单独更新 API Key。"""
    key = body.get("api_key", "").strip()
    if not key or len(key) < 8:
        raise HTTPException(400, "API Key 无效（至少 8 个字符）")
    cfg = _read_config()
    cfg["api_key"] = key
    _write_config(cfg)
    return {"status": "success", "message": "API Key 已更新"}


# ── MCP 管理端点 ─────────────────────────────────────────


@app.get("/api/mcp/servers")
async def list_mcp_servers():
    """列出所有 MCP Server（包含 enabled 状态）。"""
    cfg = _read_config()
    servers = cfg.get("mcp_servers", {})
    discovered = _discover_project_mcp_servers()
    result = []
    for name, svc in servers.items():
        result.append({
            "name": name,
            "command": svc.get("command", ""),
            "args": svc.get("args", []),
            "description": svc.get("description", ""),
            "enabled": svc.get("enabled", True),
            "env": svc.get("env", None),
            "configured": True,
            "detected": name in discovered,
            "entry_exists": discovered.get(name, {}).get("entry_exists"),
        })
    for name, svc in discovered.items():
        if name in servers:
            continue
        result.append({"name": name, **svc, "enabled": False, "env": None, "configured": False, "detected": True})
    return {"servers": result}


@app.post("/api/mcp/servers")
async def add_mcp_server(body: McpServerCreate):
    """添加 MCP Server。"""
    cfg = _read_config()
    if "mcp_servers" not in cfg:
        cfg["mcp_servers"] = {}
    if body.name in cfg["mcp_servers"]:
        raise HTTPException(409, f"Server '{body.name}' 已存在")
    cfg["mcp_servers"][body.name] = body.config.model_dump()
    _write_config(cfg)
    return {"status": "success", "message": f"MCP server '{body.name}' 已添加"}


@app.put("/api/mcp/servers/{server_name}")
async def update_mcp_server(server_name: str, body: McpServerUpdate):
    """更新 MCP Server 配置。"""
    cfg = _read_config()
    servers = cfg.get("mcp_servers", {})
    if server_name not in servers:
        raise HTTPException(404, f"Server '{server_name}' 不存在")
    servers[server_name] = body.config.model_dump()
    _write_config(cfg)
    return {"status": "success", "message": f"MCP server '{server_name}' 已更新"}


@app.delete("/api/mcp/servers/{server_name}")
async def delete_mcp_server(server_name: str):
    """删除 MCP Server。"""
    cfg = _read_config()
    servers = cfg.get("mcp_servers", {})
    if server_name not in servers:
        raise HTTPException(404, f"Server '{server_name}' 不存在")
    del servers[server_name]
    _write_config(cfg)
    return {"status": "success", "message": f"MCP server '{server_name}' 已删除"}


@app.post("/api/mcp/servers/{server_name}/toggle")
async def toggle_mcp_server(server_name: str):
    """
    切换 MCP Server 启用状态（轻量方案：仅改 config.json）。
    重启 Gateway 后生效。
    """
    cfg = _read_config()
    servers = cfg.get("mcp_servers", {})
    if server_name not in servers:
        raise HTTPException(404, f"Server '{server_name}' 不存在")
    current = servers[server_name].get("enabled", True)
    servers[server_name]["enabled"] = not current
    _write_config(cfg)
    new_state = "启用" if servers[server_name]["enabled"] else "禁用"
    return {
        "status": "success",
        "enabled": servers[server_name]["enabled"],
        "message": f"MCP server '{server_name}' 已{new_state}（重启 Gateway 后生效）",
    }


# ── Gateway 进程管理端点 ─────────────────────────────────


@app.post("/api/gateway/start")
async def start_gateway():
    """启动 Gateway。"""
    msg = await gateway.start()
    return {"status": "success" if "已启动" in msg else "error", "message": msg}


@app.post("/api/gateway/stop")
async def stop_gateway():
    """停止 Gateway。"""
    msg = await gateway.stop()
    return {"status": "success", "message": msg}


@app.get("/api/gateway/status")
async def gateway_status():
    """获取 Gateway 运行状态。"""
    status = gateway.get_status()
    cfg = _read_config().get("web", {})
    host = os.environ.get("NANOCLAW_WEB_HOST", cfg.get("host", "127.0.0.1"))
    port = int(os.environ.get("NANOCLAW_WEB_PORT", cfg.get("port", 8080)))
    ready = False
    if status.get("running"):
        try:
            with socket.create_connection((host, port), timeout=0.3):
                ready = True
        except OSError:
            pass
    status.update({"web_ready": ready, "web_url": f"http://{host}:{port}"})
    return status


@app.get("/api/gateway/web-url")
async def gateway_web_url():
    cfg = _read_config().get("web", {})
    host = os.environ.get("NANOCLAW_WEB_HOST", cfg.get("host", "127.0.0.1"))
    port = int(os.environ.get("NANOCLAW_WEB_PORT", cfg.get("port", 8080)))
    return {"url": f"http://{host}:{port}"}


# ── 日志 WebSocket ─────────────────────────────────────


@app.websocket("/ws/gateway/logs")
async def gateway_logs(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        # 先推送已有日志
        status = gateway.get_status()
        for line in status.get("logs", []):
            await websocket.send_text(line)
        # 保持连接（日志由后台任务推送）
        while True:
            await websocket.receive_text()  # ping/pong
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ── 工具端点 ────────────────────────────────────────────

# Launcher 自身配置


@app.get("/api/launcher/config")
async def get_launcher_config():
    """获取 Launcher 自己的配置。"""
    return _read_launcher_config()


@app.put("/api/launcher/config")
async def update_launcher_config(body: dict):
    """更新 Launcher 配置（python_path 等）。"""
    cfg = _read_launcher_config()
    if "python_path" in body:
        cfg["python_path"] = body["python_path"].strip()
        # 同步到 gateway_manager
        gateway.set_python_path(cfg["python_path"])
    _write_launcher_config(cfg)
    return {"status": "success", "message": "Launcher 配置已更新"}


@app.get("/api/util/python-envs")
async def scan_python_envs():
    """扫描系统上可用的 Python 环境。"""
    found = []

    # 1. PATH 上的 python
    for name in ["python", "python3"]:
        try:
            r = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                ver = r.stdout.strip() or r.stderr.strip()
                found.append({"path": name, "label": f"PATH: {name}", "version": ver, "type": "path"})
        except Exception:
            pass

    # 2. conda 环境
    for conda_cmd in ["conda", "conda.bat", "conda.exe"]:
        try:
            r = subprocess.run([conda_cmd, "info", "--envs", "--json"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                import json as _json
                info = _json.loads(r.stdout)
                for env_name, env_path in info.get("envs", {}).items() if isinstance(info.get("envs"), dict) else []:
                    pass
                # 新版 conda 返回列表
                envs = info.get("envs", [])
                if isinstance(envs, list):
                    for env_path in envs:
                        py_path = os.path.join(env_path, "python.exe") if sys.platform == "win32" else os.path.join(env_path, "bin", "python")
                        if os.path.isfile(py_path):
                            try:
                                vr = subprocess.run([py_path, "--version"], capture_output=True, text=True, timeout=5)
                                ver = vr.stdout.strip() or vr.stderr.strip()
                            except Exception:
                                ver = "?"
                            label = os.path.basename(env_path)
                            if label == "base":
                                label = f"conda: base ({env_path})"
                            else:
                                label = f"conda: {label}"
                            found.append({"path": py_path, "label": label, "version": ver, "type": "conda"})
                break
        except Exception:
            continue

    # 3. 已知的 conda 路径自动补全（即使 conda 命令不可用）
    for base in ["C:\\Users\\dell\\anaconda3", "C:\\Users\\dell\\miniconda3",
                 "C:\\ProgramData\\anaconda3", "C:\\ProgramData\\miniconda3",
                 "C:\\tools\\anaconda3", "C:\\tools\\miniconda3",
                 os.path.expanduser("~\\anaconda3"), os.path.expanduser("~\\miniconda3")]:
        envs_dir = os.path.join(base, "envs")
        base_py = os.path.join(base, "python.exe")
        if os.path.isfile(base_py):
            label = f"conda: base ({base})"
            if not any(f["path"] == base_py for f in found):
                try:
                    vr = subprocess.run([base_py, "--version"], capture_output=True, text=True, timeout=5)
                    ver = vr.stdout.strip() or vr.stderr.strip()
                except Exception:
                    ver = "?"
                found.append({"path": base_py, "label": label, "version": ver, "type": "conda"})
        if os.path.isdir(envs_dir):
            for entry in os.listdir(envs_dir):
                py_path = os.path.join(envs_dir, entry, "python.exe")
                if os.path.isfile(py_path) and not any(f["path"] == py_path for f in found):
                    try:
                        vr = subprocess.run([py_path, "--version"], capture_output=True, text=True, timeout=5)
                        ver = vr.stdout.strip() or vr.stderr.strip()
                    except Exception:
                        ver = "?"
                    found.append({"path": py_path, "label": f"conda: {entry}", "version": ver, "type": "conda"})

    # 4. 当前运行环境
    cur = sys.executable
    if not any(f["path"] == cur for f in found):
        found.insert(0, {"path": cur, "label": "当前环境 (Launcher)", "version": f"Python {sys.version.split()[0]}", "type": "current"})

    return {"environments": found, "current": _read_launcher_config().get("python_path", "")}


@app.post("/api/util/open-explorer")
async def open_explorer():
    """在文件管理器中打开 NanoClaw 工作区。"""
    path = str(NANOCLAW_DIR.resolve())
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        raise HTTPException(500, f"打开目录失败: {e}")
    return {"status": "success", "message": f"已打开 {path}"}


@app.post("/api/util/open-cli")
async def open_cli():
    """在新终端窗口中打开 NanoClaw CLI。"""
    script = str((NANOCLAW_DIR / "main.py").resolve())
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["start", "cmd", "/k", f"python \"{script}\""],
                shell=True,
                cwd=str(NANOCLAW_DIR),
            )
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", "-a", "Terminal", script],
                cwd=str(NANOCLAW_DIR),
            )
        else:
            subprocess.Popen(
                ["x-terminal-emulator", "-e", f"python {script}"],
                cwd=str(NANOCLAW_DIR),
            )
    except Exception as e:
        raise HTTPException(500, f"打开 CLI 失败: {e}")
    return {"status": "success", "message": "CLI 终端已打开"}


@app.get("/api/util/workspace-path")
async def workspace_path():
    """返回 NanoClaw 工作区路径。"""
    return {"path": str(NANOCLAW_DIR.resolve())}


# ── 入口 ─────────────────────────────────────────────────


if __name__ == "__main__":
    print(f"🌐 NanoClaw Manager 启动: http://localhost:{LAUNCHER_PORT}")
    print(f"📁 工作区: {NANOCLAW_DIR}")
    print(f"📂 管理 UI: http://localhost:{LAUNCHER_PORT}/ui/")
    webbrowser.open(f"http://localhost:{LAUNCHER_PORT}/ui/")
    uvicorn.run(app, host="127.0.0.1", port=LAUNCHER_PORT, access_log=False, log_level="warning")
