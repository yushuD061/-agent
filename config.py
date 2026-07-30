"""
NanoClaw 配置模块

提供配置加载和管理功能，支持：
- 从 JSON 文件读取配置
- 敏感值仅通过环境变量注入
- 默认值填充

配置文件使用 config.json，仅包含模型、工作区等非敏感参数。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path


_PROJECT_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_project_env() -> None:
    """加载项目根目录 .env；不覆盖 Launcher/Docker 已注入的进程环境。"""
    if not _PROJECT_ENV_PATH.is_file():
        return
    try:
        original_keys = set(os.environ)
        for raw_line in _PROJECT_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in original_keys:
                # dotenv 常规语义：同一文件内最后一项覆盖前一项。
                os.environ[key] = value
    except OSError:
        pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass
class NanoClawConfig:
    """
    NanoClaw 配置结构

    包含 Agent 运行所需的所有配置参数。

    Attributes:
        api_key: API 密钥，优先从环境变量 NANOCLAW_API_KEY 读取
        base_url: API 基础 URL，默认硅基流动地址
        model: 使用的模型名称，默认 Kimi-K2.5
        workspace: 工作区路径，默认当前目录
        max_iterations: Agent 最大迭代次数，防止无限循环
        identity_file: 人设文件名，相对于 workspace
    """

    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn/v1"
    model: str = "Pro/moonshotai/Kimi-K2.5"
    models: dict = None  # {"main": "...", "subagent": "...", "cheap": "..."}
    workspace: str = "."
    max_iterations: int = 32
    identity_file: str = "identity.md"
    customer_identity_file: str = "customer_identity.md"
    customer_session_dir: str = "workspace/customer_sessions"
    customer_memory_enabled: bool = True
    customer_memory_file: str = "workspace/customer_memory/PUBLIC_MEMORY.md"
    # Dual-Agent memory runtime (M0 contracts only; every new read/write path is off).
    customer_auth_enabled: bool = False
    customer_registration_enabled: bool = False
    customer_auth_database_path: str = "workspace/customer_auth/customer_auth.db"
    customer_data_database_path: str = "workspace/customer_data/customer_data.db"
    customer_auth_idle_minutes: int = 30
    customer_auth_absolute_hours: int = 12
    customer_anonymous_claim_enabled: bool = True
    workspace_memory_enabled: bool = False
    workspace_memory_auto_extract_enabled: bool = False
    workspace_memory_hybrid_enabled: bool = False
    workspace_memory_governance_enabled: bool = False
    workspace_memory_review_enabled: bool = False
    workspace_memory_legacy_fallback_enabled: bool = True
    workspace_memory_embedding_backend: str = "local_hash"
    workspace_memory_embedding_dimensions: int = 64
    workspace_memory_embedding_base_url: str = ""
    workspace_memory_embedding_model: str = ""
    workspace_memory_embedding_api_key: str = ""
    workspace_memory_external_transfer_approved: bool = False
    workspace_memory_semantic_half_life_days: float = 180.0
    workspace_memory_episodic_half_life_days: float = 30.0
    workspace_memory_procedural_half_life_days: float = 365.0
    workspace_memory_review_interval_hours: int = 168
    workspace_memory_maintenance_seconds: int = 3600
    workspace_memory_admin_token: str = ""
    workspace_memory_admin_allowed_origins: tuple[str, ...] = ()
    customer_long_term_memory_enabled: bool = False
    customer_conversation_memory_enabled: bool = False
    workspace_customer_memory_read_enabled: bool = False
    # Trusted process identity for the local M4 workspace reader. Never model input.
    workspace_operator_id: str = ""
    workspace_operator_tenant_id: str = ""
    memory_hybrid_retrieval_enabled: bool = False
    memory_embedding_backend: str = "local_hash"
    memory_embedding_dimensions: int = 64
    memory_hybrid_keyword_weight: float = 1.0
    memory_hybrid_vector_weight: float = 1.0
    memory_hybrid_score_threshold: float = 0.0
    # M6 governance is opt-in. Numeric defaults are local placeholders, not legal approval.
    memory_governance_enabled: bool = False
    customer_anonymous_retention_days: int = 30
    peer_memory_ttl_hours: int = 24
    memory_deletion_sla_hours: int = 72
    memory_backup_enabled: bool = False
    memory_max_turns: int = 10
    memory_recall_top_k: int = 3
    # 飞书配置
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # QQ 配置
    qq_app_id: str = ""
    qq_app_secret: str = ""
    # Web 配置
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    workspace_web_enabled: bool = False
    workspace_web_host: str = "127.0.0.1"
    workspace_web_port: int = 8767
    customer_portal_enabled: bool = True
    customer_portal_host: str = ""
    customer_portal_port: int = 8766
    customer_portal_public_url: str = ""
    web_multi_conversation_enabled: bool = True
    knowledge_admin_enabled: bool = True
    workflow_events_enabled: bool = True
    # Unified human-intervenable trade task runtime.  Local workspace default is on.
    task_runtime_enabled: bool = True
    # Internal email-administration API. Token is environment-only.
    email_admin_token: str = ""
    email_admin_allowed_origins: tuple[str, ...] = ()
    email_smtp_worker_enabled: bool = False
    email_smtp_poll_seconds: int = 5
    email_smtp_timeout_seconds: int = 20
    email_smtp_batch_size: int = 10
    # MCP Server 配置
    mcp_servers: dict = None  # {"server_name": {"command": "...", "args": [...]}}


def load_config(config_path: str = "config.json") -> NanoClawConfig:
    """
    加载配置

    从指定路径读取 JSON 配置文件，填充 NanoClawConfig 字段。
    API、飞书和 QQ 凭证仅从环境变量读取，配置文件中的同名字段会被忽略。

    Args:
        config_path: 配置文件路径，默认 config.json

    Returns:
        NanoClawConfig: 配置对象

    注意：
        - 配置文件不存在时返回默认配置
        - 所有敏感值只接受环境变量注入
    """
    _load_project_env()

    # 默认配置
    config = NanoClawConfig()

    # 从 JSON 文件读取（如果存在）
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 更新配置字段（仅更新存在的字段）
            if "base_url" in data:
                config.base_url = data["base_url"]
            if "model" in data:
                config.model = data["model"]
            if "workspace" in data:
                config.workspace = data["workspace"]
            if "max_iterations" in data:
                config.max_iterations = data["max_iterations"]
            if "identity_file" in data:
                config.identity_file = data["identity_file"]
            # 加载多模型配置
            if "models" in data:
                config.models = data["models"]
                # 如果没有单独的 model 字段，用 models.main
                if "model" not in data and "main" in config.models:
                    config.model = config.models["main"]
            # 加载 Web 配置
            web = data.get("web", {})
            if "enabled" in web:
                config.web_enabled = web["enabled"]
            if web.get("host"):
                config.web_host = web["host"]
            if web.get("port"):
                config.web_port = web["port"]
            workspace_web = data.get("workspace_web", {})
            if "enabled" in workspace_web:
                config.workspace_web_enabled = bool(workspace_web["enabled"])
            if workspace_web.get("host"):
                config.workspace_web_host = workspace_web["host"]
            if workspace_web.get("port"):
                config.workspace_web_port = int(workspace_web["port"])
            customer_portal = data.get("customer_portal", {})
            if "enabled" in customer_portal:
                config.customer_portal_enabled = customer_portal["enabled"]
            if customer_portal.get("host"):
                config.customer_portal_host = customer_portal["host"]
            if customer_portal.get("port"):
                config.customer_portal_port = customer_portal["port"]
            if customer_portal.get("public_url"):
                config.customer_portal_public_url = customer_portal["public_url"].rstrip("/")
            if customer_portal.get("identity_file"):
                config.customer_identity_file = customer_portal["identity_file"]
            if customer_portal.get("session_dir"):
                config.customer_session_dir = customer_portal["session_dir"]
            if "memory_enabled" in customer_portal:
                config.customer_memory_enabled = bool(customer_portal["memory_enabled"])
            if customer_portal.get("memory_file"):
                config.customer_memory_file = customer_portal["memory_file"]
            customer_auth = data.get("customer_auth", {})
            if "enabled" in customer_auth:
                config.customer_auth_enabled = bool(customer_auth["enabled"])
            if "registration_enabled" in customer_auth:
                config.customer_registration_enabled = bool(
                    customer_auth["registration_enabled"]
                )
            if customer_auth.get("database_path"):
                config.customer_auth_database_path = customer_auth["database_path"]
            if "idle_minutes" in customer_auth:
                config.customer_auth_idle_minutes = int(customer_auth["idle_minutes"])
            if "absolute_hours" in customer_auth:
                config.customer_auth_absolute_hours = int(customer_auth["absolute_hours"])
            if "anonymous_claim_enabled" in customer_auth:
                config.customer_anonymous_claim_enabled = bool(
                    customer_auth["anonymous_claim_enabled"]
                )
            memory_runtime = data.get("memory_runtime", {})
            if memory_runtime.get("customer_data_database_path"):
                config.customer_data_database_path = memory_runtime[
                    "customer_data_database_path"
                ]
            if "workspace_enabled" in memory_runtime:
                config.workspace_memory_enabled = bool(
                    memory_runtime["workspace_enabled"]
                )
            if "workspace_auto_extract_enabled" in memory_runtime:
                config.workspace_memory_auto_extract_enabled = bool(
                    memory_runtime["workspace_auto_extract_enabled"]
                )
            if "workspace_hybrid_enabled" in memory_runtime:
                config.workspace_memory_hybrid_enabled = bool(
                    memory_runtime["workspace_hybrid_enabled"]
                )
            if "workspace_governance_enabled" in memory_runtime:
                config.workspace_memory_governance_enabled = bool(
                    memory_runtime["workspace_governance_enabled"]
                )
            if "workspace_review_enabled" in memory_runtime:
                config.workspace_memory_review_enabled = bool(
                    memory_runtime["workspace_review_enabled"]
                )
            if "customer_long_term_enabled" in memory_runtime:
                config.customer_long_term_memory_enabled = bool(
                    memory_runtime["customer_long_term_enabled"]
                )
            if "customer_conversation_enabled" in memory_runtime:
                config.customer_conversation_memory_enabled = bool(
                    memory_runtime["customer_conversation_enabled"]
                )
            if "workspace_customer_read_enabled" in memory_runtime:
                config.workspace_customer_memory_read_enabled = bool(
                    memory_runtime["workspace_customer_read_enabled"]
                )
            if "hybrid_retrieval_enabled" in memory_runtime:
                config.memory_hybrid_retrieval_enabled = bool(
                    memory_runtime["hybrid_retrieval_enabled"]
                )
            if "governance_enabled" in memory_runtime:
                config.memory_governance_enabled = bool(memory_runtime["governance_enabled"])
            if "backup_enabled" in memory_runtime:
                config.memory_backup_enabled = bool(memory_runtime["backup_enabled"])
            for key, attr in (
                ("anonymous_retention_days", "customer_anonymous_retention_days"),
                ("peer_ttl_hours", "peer_memory_ttl_hours"),
                ("deletion_sla_hours", "memory_deletion_sla_hours"),
            ):
                if key in memory_runtime:
                    setattr(config, attr, int(memory_runtime[key]))
            if "max_turns" in memory_runtime:
                config.memory_max_turns = int(memory_runtime["max_turns"])
            if "recall_top_k" in memory_runtime:
                config.memory_recall_top_k = int(memory_runtime["recall_top_k"])
            # 加载 MCP Server 配置
            if "mcp_servers" in data:
                config.mcp_servers = {
                    name: server
                    for name, server in data["mcp_servers"].items()
                    if server.get("enabled", True)
                }

        except json.JSONDecodeError:
            # JSON 解析错误，使用默认配置
            pass
        except Exception:
            # 其他错误，使用默认配置
            pass

    # 环境变量优先级最高
    # Sensitive values are accepted exclusively from the process environment.
    # Deliberately ignore legacy credential fields that may exist in JSON files.
    config.api_key = os.environ.get("NANOCLAW_API_KEY", "")
    config.feishu_app_id = os.environ.get("NANOCLAW_FEISHU_APP_ID", "")
    config.feishu_app_secret = os.environ.get("NANOCLAW_FEISHU_APP_SECRET", "")
    config.qq_app_id = os.environ.get("NANOCLAW_QQ_APP_ID", "")
    config.qq_app_secret = os.environ.get("NANOCLAW_QQ_APP_SECRET", "")
    config.web_host = os.environ.get("NANOCLAW_WEB_HOST", config.web_host)
    try:
        config.web_port = int(os.environ.get("NANOCLAW_WEB_PORT", config.web_port))
    except ValueError:
        raise ValueError("NANOCLAW_WEB_PORT must be an integer")
    config.workspace_web_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_WEB_ENABLED", config.workspace_web_enabled)
    config.workspace_web_host = os.environ.get(
        "NANOCLAW_WORKSPACE_WEB_HOST", config.workspace_web_host)
    try:
        config.workspace_web_port = int(os.environ.get(
            "NANOCLAW_WORKSPACE_WEB_PORT", config.workspace_web_port))
    except ValueError:
        raise ValueError("NANOCLAW_WORKSPACE_WEB_PORT must be an integer")
    config.customer_portal_enabled = _env_bool(
        "NANOCLAW_CUSTOMER_PORTAL_ENABLED", config.customer_portal_enabled)
    config.customer_portal_host = os.environ.get(
        "NANOCLAW_CUSTOMER_PORTAL_HOST", config.customer_portal_host or config.web_host)
    try:
        config.customer_portal_port = int(os.environ.get(
            "NANOCLAW_CUSTOMER_PORTAL_PORT", config.customer_portal_port))
    except ValueError:
        raise ValueError("NANOCLAW_CUSTOMER_PORTAL_PORT must be an integer")
    config.customer_portal_public_url = os.environ.get(
        "NANOCLAW_CUSTOMER_PORTAL_PUBLIC_URL", config.customer_portal_public_url).rstrip("/")
    config.customer_identity_file = os.environ.get(
        "NANOCLAW_CUSTOMER_IDENTITY_FILE", config.customer_identity_file)
    config.customer_session_dir = os.environ.get(
        "NANOCLAW_CUSTOMER_SESSION_DIR", config.customer_session_dir)
    config.customer_memory_enabled = _env_bool(
        "NANOCLAW_CUSTOMER_MEMORY_ENABLED", config.customer_memory_enabled)
    config.customer_memory_file = os.environ.get(
        "NANOCLAW_CUSTOMER_MEMORY_FILE", config.customer_memory_file)
    config.customer_auth_enabled = _env_bool(
        "NANOCLAW_CUSTOMER_AUTH_ENABLED", config.customer_auth_enabled)
    config.customer_registration_enabled = _env_bool(
        "NANOCLAW_CUSTOMER_REGISTRATION_ENABLED",
        config.customer_registration_enabled,
    )
    config.customer_auth_database_path = os.environ.get(
        "NANOCLAW_CUSTOMER_AUTH_DATABASE_PATH", config.customer_auth_database_path)
    config.customer_data_database_path = os.environ.get(
        "NANOCLAW_CUSTOMER_DATA_DATABASE_PATH", config.customer_data_database_path)
    config.customer_anonymous_claim_enabled = _env_bool(
        "NANOCLAW_CUSTOMER_ANONYMOUS_CLAIM_ENABLED",
        config.customer_anonymous_claim_enabled,
    )
    config.workspace_memory_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_MEMORY_ENABLED", config.workspace_memory_enabled)
    config.workspace_memory_auto_extract_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_MEMORY_AUTO_EXTRACT_ENABLED",
        config.workspace_memory_auto_extract_enabled)
    config.workspace_memory_hybrid_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_MEMORY_HYBRID_ENABLED",
        config.workspace_memory_hybrid_enabled)
    config.workspace_memory_governance_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_MEMORY_GOVERNANCE_ENABLED",
        config.workspace_memory_governance_enabled)
    config.workspace_memory_review_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_MEMORY_REVIEW_ENABLED",
        config.workspace_memory_review_enabled)
    config.workspace_memory_legacy_fallback_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_MEMORY_LEGACY_FALLBACK_ENABLED",
        config.workspace_memory_legacy_fallback_enabled)
    config.workspace_memory_embedding_backend = os.environ.get(
        "NANOCLAW_WORKSPACE_MEMORY_EMBEDDING_BACKEND",
        config.workspace_memory_embedding_backend).strip().lower()
    config.workspace_memory_embedding_base_url = os.environ.get(
        "NANOCLAW_WORKSPACE_MEMORY_EMBEDDING_BASE_URL", "").strip()
    config.workspace_memory_embedding_model = os.environ.get(
        "NANOCLAW_WORKSPACE_MEMORY_EMBEDDING_MODEL", "").strip()
    config.workspace_memory_embedding_api_key = os.environ.get(
        "NANOCLAW_WORKSPACE_MEMORY_EMBEDDING_API_KEY", "").strip()
    config.workspace_memory_external_transfer_approved = _env_bool(
        "NANOCLAW_WORKSPACE_MEMORY_EXTERNAL_TRANSFER_APPROVED", False)
    config.workspace_memory_admin_token = os.environ.get(
        "NANOCLAW_WORKSPACE_MEMORY_ADMIN_TOKEN", "").strip()
    config.workspace_memory_admin_allowed_origins = tuple(
        value.strip().rstrip("/") for value in os.environ.get(
            "NANOCLAW_WORKSPACE_MEMORY_ADMIN_ALLOWED_ORIGINS", ""
        ).split(",") if value.strip()
    )
    config.customer_long_term_memory_enabled = _env_bool(
        "NANOCLAW_CUSTOMER_LONG_TERM_MEMORY_ENABLED",
        config.customer_long_term_memory_enabled,
    )
    config.customer_conversation_memory_enabled = _env_bool(
        "NANOCLAW_CUSTOMER_CONVERSATION_MEMORY_ENABLED",
        config.customer_conversation_memory_enabled,
    )
    config.workspace_customer_memory_read_enabled = _env_bool(
        "NANOCLAW_WORKSPACE_CUSTOMER_MEMORY_READ_ENABLED",
        config.workspace_customer_memory_read_enabled,
    )
    config.workspace_operator_id = os.environ.get(
        "NANOCLAW_WORKSPACE_OPERATOR_ID", config.workspace_operator_id,
    ).strip()
    config.workspace_operator_tenant_id = os.environ.get(
        "NANOCLAW_WORKSPACE_OPERATOR_TENANT_ID",
        config.workspace_operator_tenant_id,
    ).strip()
    config.memory_hybrid_retrieval_enabled = _env_bool(
        "NANOCLAW_MEMORY_HYBRID_RETRIEVAL_ENABLED",
        config.memory_hybrid_retrieval_enabled,
    )
    config.memory_governance_enabled = _env_bool(
        "NANOCLAW_MEMORY_GOVERNANCE_ENABLED", config.memory_governance_enabled)
    config.memory_backup_enabled = _env_bool(
        "NANOCLAW_MEMORY_BACKUP_ENABLED", config.memory_backup_enabled)
    config.memory_embedding_backend = os.environ.get(
        "NANOCLAW_MEMORY_EMBEDDING_BACKEND", config.memory_embedding_backend,
    ).strip().lower()
    try:
        config.customer_auth_idle_minutes = int(os.environ.get(
            "NANOCLAW_CUSTOMER_AUTH_IDLE_MINUTES",
            config.customer_auth_idle_minutes,
        ))
        config.customer_auth_absolute_hours = int(os.environ.get(
            "NANOCLAW_CUSTOMER_AUTH_ABSOLUTE_HOURS",
            config.customer_auth_absolute_hours,
        ))
        config.memory_max_turns = int(os.environ.get(
            "NANOCLAW_MEMORY_MAX_TURNS", config.memory_max_turns,
        ))
        config.memory_recall_top_k = int(os.environ.get(
            "NANOCLAW_MEMORY_RECALL_TOP_K", config.memory_recall_top_k,
        ))
        config.memory_embedding_dimensions = int(os.environ.get(
            "NANOCLAW_MEMORY_EMBEDDING_DIMENSIONS",
            config.memory_embedding_dimensions,
        ))
        config.workspace_memory_embedding_dimensions = int(os.environ.get(
            "NANOCLAW_WORKSPACE_MEMORY_EMBEDDING_DIMENSIONS",
            config.workspace_memory_embedding_dimensions,
        ))
        config.workspace_memory_review_interval_hours = int(os.environ.get(
            "NANOCLAW_WORKSPACE_MEMORY_REVIEW_INTERVAL_HOURS",
            config.workspace_memory_review_interval_hours,
        ))
        config.workspace_memory_maintenance_seconds = int(os.environ.get(
            "NANOCLAW_WORKSPACE_MEMORY_MAINTENANCE_SECONDS",
            config.workspace_memory_maintenance_seconds,
        ))
        config.customer_anonymous_retention_days = int(os.environ.get(
            "NANOCLAW_CUSTOMER_ANONYMOUS_RETENTION_DAYS",
            config.customer_anonymous_retention_days,
        ))
        config.peer_memory_ttl_hours = int(os.environ.get(
            "NANOCLAW_PEER_MEMORY_TTL_HOURS", config.peer_memory_ttl_hours,
        ))
        config.memory_deletion_sla_hours = int(os.environ.get(
            "NANOCLAW_MEMORY_DELETION_SLA_HOURS", config.memory_deletion_sla_hours,
        ))
    except ValueError as exc:
        raise ValueError("memory runtime numeric settings must be integers") from exc
    try:
        config.memory_hybrid_keyword_weight = float(os.environ.get(
            "NANOCLAW_MEMORY_HYBRID_KEYWORD_WEIGHT",
            config.memory_hybrid_keyword_weight,
        ))
        config.memory_hybrid_vector_weight = float(os.environ.get(
            "NANOCLAW_MEMORY_HYBRID_VECTOR_WEIGHT",
            config.memory_hybrid_vector_weight,
        ))
        config.memory_hybrid_score_threshold = float(os.environ.get(
            "NANOCLAW_MEMORY_HYBRID_SCORE_THRESHOLD",
            config.memory_hybrid_score_threshold,
        ))
        config.workspace_memory_semantic_half_life_days = float(os.environ.get(
            "NANOCLAW_WORKSPACE_MEMORY_SEMANTIC_HALF_LIFE_DAYS",
            config.workspace_memory_semantic_half_life_days,
        ))
        config.workspace_memory_episodic_half_life_days = float(os.environ.get(
            "NANOCLAW_WORKSPACE_MEMORY_EPISODIC_HALF_LIFE_DAYS",
            config.workspace_memory_episodic_half_life_days,
        ))
        config.workspace_memory_procedural_half_life_days = float(os.environ.get(
            "NANOCLAW_WORKSPACE_MEMORY_PROCEDURAL_HALF_LIFE_DAYS",
            config.workspace_memory_procedural_half_life_days,
        ))
    except ValueError as exc:
        raise ValueError("memory hybrid settings must be numeric") from exc
    if min(
        config.customer_auth_idle_minutes,
        config.customer_auth_absolute_hours,
        config.memory_max_turns,
        config.memory_recall_top_k,
        config.memory_embedding_dimensions,
        config.workspace_memory_embedding_dimensions,
        config.workspace_memory_review_interval_hours,
        config.workspace_memory_maintenance_seconds,
        config.customer_anonymous_retention_days,
        config.peer_memory_ttl_hours,
        config.memory_deletion_sla_hours,
    ) < 1:
        raise ValueError("memory runtime numeric settings must be positive")
    if min(
        config.workspace_memory_semantic_half_life_days,
        config.workspace_memory_episodic_half_life_days,
        config.workspace_memory_procedural_half_life_days,
    ) <= 0:
        raise ValueError("workspace memory half lives must be positive")
    if config.workspace_memory_review_enabled and not (
        config.workspace_memory_enabled and config.workspace_memory_governance_enabled
    ):
        raise ValueError(
            "workspace memory review requires workspace memory and governance"
        )
    if config.workspace_memory_enabled and not (
        config.workspace_operator_id and config.workspace_operator_tenant_id
    ):
        raise ValueError(
            "workspace memory requires trusted workspace operator identity"
        )
    if config.workspace_memory_embedding_backend not in {"local_hash", "openai_compatible"}:
        raise ValueError("workspace memory embedding backend is invalid")
    if config.workspace_memory_embedding_backend == "openai_compatible":
        if not config.workspace_memory_external_transfer_approved:
            raise ValueError("workspace memory external transfer requires explicit approval")
        if not all((config.workspace_memory_embedding_base_url,
                    config.workspace_memory_embedding_model,
                    config.workspace_memory_embedding_api_key)):
            raise ValueError("workspace memory external embedding configuration is incomplete")
    if config.customer_conversation_memory_enabled and not config.customer_auth_enabled:
        raise ValueError(
            "customer conversation memory requires NANOCLAW_CUSTOMER_AUTH_ENABLED"
        )
    if config.customer_long_term_memory_enabled and not config.customer_conversation_memory_enabled:
        raise ValueError(
            "customer long-term memory requires NANOCLAW_CUSTOMER_CONVERSATION_MEMORY_ENABLED"
        )
    if config.workspace_customer_memory_read_enabled:
        if not config.customer_long_term_memory_enabled:
            raise ValueError(
                "workspace customer-memory read requires "
                "NANOCLAW_CUSTOMER_LONG_TERM_MEMORY_ENABLED"
            )
        if not config.workspace_operator_id or not config.workspace_operator_tenant_id:
            raise ValueError(
                "workspace customer-memory read requires trusted workspace operator identity"
            )
    if config.memory_hybrid_retrieval_enabled:
        if not config.customer_long_term_memory_enabled:
            raise ValueError(
                "hybrid memory retrieval requires NANOCLAW_CUSTOMER_LONG_TERM_MEMORY_ENABLED"
            )
        if config.memory_embedding_backend != "local_hash":
            raise ValueError(
                "external memory embedding backend requires explicit approved adapter"
            )
        if (
            config.memory_hybrid_keyword_weight < 0
            or config.memory_hybrid_vector_weight < 0
            or not (config.memory_hybrid_keyword_weight or config.memory_hybrid_vector_weight)
            or config.memory_hybrid_score_threshold < 0
        ):
            raise ValueError("memory hybrid retrieval settings are invalid")
    if config.memory_governance_enabled and not config.customer_long_term_memory_enabled:
        raise ValueError(
            "memory governance requires NANOCLAW_CUSTOMER_LONG_TERM_MEMORY_ENABLED"
        )
    if config.memory_backup_enabled and not config.memory_governance_enabled:
        raise ValueError("memory backup requires NANOCLAW_MEMORY_GOVERNANCE_ENABLED")
    if min(config.web_port, config.workspace_web_port, config.customer_portal_port) < 1:
        raise ValueError("web ports must be positive")
    if config.web_enabled and config.customer_portal_enabled:
        same_host = config.customer_portal_host in {config.web_host, "0.0.0.0", "::"}
        if same_host and config.customer_portal_port == config.web_port:
            raise ValueError("customer portal port must differ from the workspace web port")
    if config.workspace_web_enabled:
        if config.web_enabled and config.workspace_web_port == config.web_port:
            raise ValueError("dedicated workspace port must differ from the public web port")
        if (config.customer_portal_enabled
                and config.workspace_web_port == config.customer_portal_port):
            raise ValueError("dedicated workspace port must differ from the customer portal port")
    config.web_multi_conversation_enabled = _env_bool(
        "WEB_MULTI_CONVERSATION_ENABLED", config.web_multi_conversation_enabled)
    config.knowledge_admin_enabled = _env_bool(
        "KNOWLEDGE_ADMIN_ENABLED", config.knowledge_admin_enabled)
    config.workflow_events_enabled = _env_bool(
        "WORKFLOW_EVENTS_ENABLED", config.workflow_events_enabled)
    config.task_runtime_enabled = _env_bool(
        "NANOCLAW_TASK_RUNTIME_ENABLED", config.task_runtime_enabled)
    config.email_admin_token = os.environ.get("NANOCLAW_EMAIL_ADMIN_TOKEN", "")
    config.email_admin_allowed_origins = tuple(
        value.strip().rstrip("/")
        for value in os.environ.get("NANOCLAW_EMAIL_ADMIN_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    )
    config.email_smtp_worker_enabled = _env_bool(
        "NANOCLAW_EMAIL_SMTP_WORKER_ENABLED", config.email_smtp_worker_enabled)
    try:
        config.email_smtp_poll_seconds = int(os.environ.get(
            "NANOCLAW_EMAIL_SMTP_POLL_SECONDS", config.email_smtp_poll_seconds))
        config.email_smtp_timeout_seconds = int(os.environ.get(
            "NANOCLAW_EMAIL_SMTP_TIMEOUT_SECONDS", config.email_smtp_timeout_seconds))
        config.email_smtp_batch_size = int(os.environ.get(
            "NANOCLAW_EMAIL_SMTP_BATCH_SIZE", config.email_smtp_batch_size))
    except ValueError as exc:
        raise ValueError("SMTP worker numeric settings must be integers") from exc
    if not 1 <= config.email_smtp_poll_seconds <= 300:
        raise ValueError("NANOCLAW_EMAIL_SMTP_POLL_SECONDS must be between 1 and 300")
    if not 1 <= config.email_smtp_timeout_seconds <= 120:
        raise ValueError("NANOCLAW_EMAIL_SMTP_TIMEOUT_SECONDS must be between 1 and 120")
    if not 1 <= config.email_smtp_batch_size <= 100:
        raise ValueError("NANOCLAW_EMAIL_SMTP_BATCH_SIZE must be between 1 and 100")

    return config
