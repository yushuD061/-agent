"""
NanoClaw 外贸业务模块配置

从 business_config.json 加载非敏感业务配置。
RFQ 抽取与 NanoClaw 主 Agent 统一使用 NANOCLAW_API_KEY。
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BusinessConfig:
    """业务模块配置"""
    database_backend: str = "sqlite"
    database_path: str = "data/business.db"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "trade_ops"
    mysql_user: str = ""
    mysql_password: str = ""
    api_key: str = ""
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "Qwen/Qwen3.5-35B-A3B"
    exchange_rates: dict = field(default_factory=lambda: {"USD": 1.0})
    default_markup_percent: float = 15.0
    default_validity_days: int = 15
    max_rfq_text_length: int = 10000


_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "business_config.json")
_PROJECT_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(_CONFIG_DIR)), ".env")
_loaded: Optional[BusinessConfig] = None


def _load_project_env() -> None:
    """Load simple KEY=VALUE entries without overriding process/Docker env."""
    if not os.path.isfile(_PROJECT_ENV_PATH):
        return
    try:
        with open(_PROJECT_ENV_PATH, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except OSError:
        pass


def load_business_config() -> BusinessConfig:
    """加载业务模块配置（单例缓存）"""
    global _loaded
    if _loaded is not None:
        return _loaded

    _load_project_env()
    cfg = BusinessConfig()

    if os.path.isfile(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            database = data.get("database", {})
            cfg.database_backend = database.get("backend", cfg.database_backend)
            db_path = data.get("database_path")
            if db_path:
                cfg.database_path = db_path if os.path.isabs(db_path) else os.path.normpath(os.path.join(_CONFIG_DIR, db_path))
            cfg.mysql_host = database.get("host", cfg.mysql_host)
            cfg.mysql_port = int(database.get("port", cfg.mysql_port))
            cfg.mysql_database = database.get("name", cfg.mysql_database)
            cfg.mysql_user = database.get("user", cfg.mysql_user)
            cfg.mysql_password = database.get("password", cfg.mysql_password)

            llm = data.get("llm", {})
            if llm.get("base_url"):
                cfg.llm_base_url = llm["base_url"]
            if llm.get("model"):
                cfg.llm_model = llm["model"]

            if "exchange_rates" in data:
                cfg.exchange_rates = data["exchange_rates"]
            if "default_markup_percent" in data:
                cfg.default_markup_percent = float(data["default_markup_percent"])
            if "default_validity_days" in data:
                cfg.default_validity_days = int(data["default_validity_days"])
            if "max_rfq_text_length" in data:
                cfg.max_rfq_text_length = int(data["max_rfq_text_length"])

        except (json.JSONDecodeError, OSError):
            pass

    # 环境变量覆盖
    cfg.api_key = os.environ.get("NANOCLAW_API_KEY", "")
    backend = os.environ.get("BUSINESS_DATABASE_BACKEND", cfg.database_backend).strip().lower()
    if backend not in {"sqlite", "mysql"}:
        raise ValueError("BUSINESS_DATABASE_BACKEND must be 'sqlite' or 'mysql'")
    cfg.database_backend = backend
    sqlite_path = os.environ.get("BUSINESS_SQLITE_PATH")
    if sqlite_path:
        cfg.database_path = sqlite_path if os.path.isabs(sqlite_path) else os.path.normpath(os.path.join(_CONFIG_DIR, sqlite_path))
    cfg.mysql_host = os.environ.get("TRADE_OPS_MYSQL_HOST", cfg.mysql_host)
    cfg.mysql_port = int(os.environ.get("TRADE_OPS_MYSQL_PORT", cfg.mysql_port))
    cfg.mysql_database = os.environ.get("TRADE_OPS_MYSQL_DATABASE", cfg.mysql_database)
    cfg.mysql_user = os.environ.get("TRADE_OPS_MYSQL_USER", cfg.mysql_user)
    cfg.mysql_password = os.environ.get("TRADE_OPS_MYSQL_PASSWORD", cfg.mysql_password)

    _loaded = cfg
    return cfg


def reload_business_config() -> BusinessConfig:
    """重新加载配置（清除缓存）"""
    global _loaded
    _loaded = None
    return load_business_config()
