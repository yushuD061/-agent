"""
Gateway 进程管理器

管理 NanoClaw Gateway (main.py) 的子进程生命周期，
捕获 stdout/stderr 并存入环形缓冲区供 WebSocket 推送。
"""

import asyncio
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

MAX_LOG_LINES = 2000
NANOCLAW_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GATEWAY_SCRIPT = os.path.join(NANOCLAW_DIR, "main.py")

# Launcher 配置文件路径
LAUNCHER_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "launcher_config.json")


def _read_launcher_config() -> dict:
    """读取 launcher 配置中的 python_path。"""
    defaults = {"python_path": ""}
    if not os.path.isfile(LAUNCHER_CONFIG_PATH):
        return defaults
    try:
        with open(LAUNCHER_CONFIG_PATH, "r", encoding="utf-8") as f:
            import json
            data = json.load(f)
            return {**defaults, **data}
    except Exception:
        return defaults


def _resolve_python() -> str:
    """返回用于启动 Gateway 的 Python 可执行文件路径。
    
    优先级：
    1. launcher_config.json 中的 python_path（用户配置）
    2. 当前运行环境的 sys.executable
    3. 回退到 "python"
    """
    cfg = _read_launcher_config()
    user_path = cfg.get("python_path", "")
    if user_path and os.path.isfile(user_path):
        return user_path
    return sys.executable or "python"


@dataclass
class GatewayProcess:
    """封装 Gateway 子进程的状态。"""

    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    start_time: Optional[float] = None
    stop_requested: bool = False
    # 环形日志缓冲区
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))

    @property
    def is_running(self) -> bool:
        if self.process is None:
            return False
        return self.process.returncode is None

    @property
    def uptime(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    async def start(self) -> str:
        """启动 Gateway 子进程。如果已在运行则返回错误消息。"""
        if self.is_running:
            return "Gateway 已在运行中"

        self.stop_requested = False
        self.log_buffer.clear()

        # 检查 main.py 是否存在
        if not os.path.isfile(GATEWAY_SCRIPT):
            msg = f"错误: 未找到 {GATEWAY_SCRIPT}"
            self.log_buffer.append(msg)
            return msg

        try:
            python_exe = _resolve_python()
            self.log_buffer.append(f"使用 Python: {python_exe}")
            # 强制子进程用 UTF-8 输出，防止 Windows GBK 编码炸 emoji
            subprocess_env = os.environ.copy()
            subprocess_env["PYTHONIOENCODING"] = "utf-8"
            self.process = await asyncio.create_subprocess_exec(
                python_exe,
                GATEWAY_SCRIPT,
                cwd=NANOCLAW_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=subprocess_env,
            )
        except Exception as e:
            msg = f"启动 Gateway 失败: {e}"
            self.log_buffer.append(msg)
            return msg

        self.pid = self.process.pid
        self.start_time = time.time()

        msg = f"Gateway 已启动 (PID: {self.pid})"
        self.log_buffer.append(msg)

        # 后台任务：持续读取 stdout/stderr
        asyncio.create_task(self._read_stream(self.process.stdout, "stdout"))
        asyncio.create_task(self._read_stream(self.process.stderr, "stderr"))

        return msg

    async def stop(self) -> str:
        """停止 Gateway 子进程。"""
        if not self.is_running:
            return "Gateway 未在运行"

        self.stop_requested = True
        pid = self.process.pid

        try:
            if sys.platform == "win32":
                # Windows 上用 terminate() + 等待
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            else:
                # Unix 上先 SIGTERM，等 5 秒再 SIGKILL
                self.process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.process.send_signal(signal.SIGKILL)
                    await self.process.wait()
        except ProcessLookupError:
            pass  # 进程已退出

        self.process = None
        self.pid = None
        self.start_time = None

        msg = f"Gateway 已停止 (PID: {pid})"
        self.log_buffer.append(msg)
        return msg

    async def _read_stream(self, stream: asyncio.StreamReader, source: str) -> None:
        """持续读取子进程的输出流，存入日志缓冲区。
        
        自动探测编码（优先 UTF-8，回退 GBK/系统编码），
        解决 Windows 中文环境下输出乱码的问题。
        """
        import locale
        _fallback_encodings = ["utf-8", "gbk", "gb2312", locale.getpreferredencoding()]
        # 去重且有序
        seen = set()
        _encodings = [e for e in _fallback_encodings if not (e in seen or seen.add(e))]
        del seen

        def _decode(data: bytes) -> str:
            for enc in _encodings:
                try:
                    return data.decode(enc).rstrip("\n\r")
                except (UnicodeDecodeError, LookupError):
                    continue
            return data.decode("utf-8", errors="replace").rstrip("\n\r")

        try:
            while not self.stop_requested and stream:
                line = await stream.readline()
                if not line:
                    break
                text = _decode(line)
                if text:
                    self.log_buffer.append(text)
        except Exception:
            pass

    def set_python_path(self, path: str) -> None:
        """设置用于启动 Gateway 的 Python 路径（运行时生效，不持久化）。"""
        self._custom_python = path
        self.log_buffer.append(f"Python 路径已设置为: {path or '（默认）'}")

    def get_python_path(self) -> str:
        """获取当前 Python 路径配置。"""
        return getattr(self, "_custom_python", "") or _resolve_python()

    def get_status(self) -> dict:
        """获取当前状态字典。"""
        running = self.is_running
        logs = list(self.log_buffer)
        return {
            "running": running,
            "pid": self.pid if running else None,
            "uptime": round(self.uptime, 1) if running else 0,
            "logs": logs,
        }


# 全局单例
_gateway = GatewayProcess()


def get_gateway() -> GatewayProcess:
    return _gateway


# 避免在模块级别导入 sys
import sys
