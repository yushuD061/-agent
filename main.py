"""
NanoClaw 入口文件

提供命令行交互界面，组装 Agent 各组件并启动对话循环。

使用方法：
    python main.py

命令：
    /exit   - 退出程序
    /clear  - 清空对话历史
    /tools  - 查看可用工具列表
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from privacy import configure_privacy_logging, safe_print as print

configure_privacy_logging()

from config import load_config
from providers.openai_compat import OpenAICompatProvider
from agent.tools.registry import ToolRegistry
from agent.tools.filesystem import ReadFileTool, WriteFileTool, ListDirTool
from agent.tools.shell import ExecTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.query_email import QueryInboundEmailTool
from agent.tools.customer_public import CustomerPublicCatalogTool, CustomerPublicKnowledgeTool
from agent.tools.workspace_peer import WorkspaceKnowledgeAnalysisTool, WorkspaceProductAnalysisTool
from agent.tools.web_search import WebSearchTool
from agent.tools.spawn import SpawnSubagentTool
from agent.tools.mcp_server import MCPClientManager
from agent.skills import SkillsLoader
from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.customer_agent import CustomerAgent
from session.manager import SessionManager
from agent.memory import MemoryConsolidator
from bus.queue import MessageBus
from channels.cli import CLIChannel
from channels.feishu import FeishuChannel
from channels.qq import QQChannel
from channels.web import WebChannel
from trade_rag.knowledge_repository import KnowledgeRepository
from trade_rag.pdf_ingestion import PdfIngestionCoordinator
from trade_rag.pipeline import RagPipeline
from channels.customer_portal import CustomerPortalChannel
from agent.customer_identity import (
    Argon2PasswordHasher,
    CustomerIdentityRepository,
    CustomerIdentityService,
    load_or_create_customer_session_secret,
)
from session.customer_conversation import CustomerConversationRepository
from session.conversation import ConversationService
from session.mysql_conversation import (
    MySQLCustomerConversationRepository, MySQLWorkspaceConversationService,
    conversation_backend,
)
from session.bounded_manager import BoundedSessionManager
from agent.memory_runtime.compaction import ProviderTurnSummarizer
from agent.memory_runtime.context_provider import StructuredWorkingMemoryContextProvider
from agent.memory_runtime.lifecycle import BoundedWorkingMemoryLifecycle
from agent.memory_runtime.models import ActorContext, MemoryScope, TurnRequest
from agent.memory_runtime.working_memory import (
    CustomerWorkingMemoryStore, WorkspaceWorkingMemoryStore,
)
from agent.memory_runtime.stores.sqlite import (
    CustomerSQLiteMemoryStore, PublicApprovedMemoryStore,
    WorkspaceSQLiteMemoryStore,
)
from agent.memory_runtime.services.customer_memory import CustomerMemoryService
from agent.memory_runtime.services.customer_reader import (
    CustomerMemoryAccessAudit, CustomerMemoryReader, ReadonlyCustomerMemoryStore,
)
from agent.memory_runtime.retrieval import HybridMemoryRetriever
from agent.memory_runtime.outbox import MemoryIndexWorker
from agent.memory_runtime.stores.keyword import KeywordMemoryIndex
from agent.memory_runtime.stores.vector import LocalHashEmbeddingAdapter, VectorMemoryIndex
from agent.memory_runtime.stores.vector import OpenAICompatibleEmbeddingAdapter
from agent.memory_runtime.services.workspace_memory import (
    DecayAwareRetriever,
    WorkspaceMemoryExtractor,
    WorkspaceMemoryLifecycle,
    WorkspaceMemoryReviewService,
    WorkspaceMemoryService,
    stable_project_id,
)
from agent.memory_runtime.workspace_commands import WorkspaceMemoryCommandRouter
from channels.workspace_memory_api import create_workspace_memory_router
from agent.tools.read_customer_memory import ReadCustomerMemoryTool
from gateway import Gateway
from gateway_coordination import coordinator_from_environment
from agent.workflow import WorkflowService
from agent.business.task_runtime_repository import TaskOwner, create_task_runtime_repository
from agent.business.task_runtime_service import TaskRuntimeService, TaskRuntimeWorker
from agent.peer_coordination import WorkspacePeerCoordinator
from agent.business.email_config import load_email_config
from agent.business.email_repository import EmailRepository
from agent.business.email_notification import QQNotificationDispatcher
from agent.business.email_ingestion import EmailIngestionService
from agent.business.managed_email_ingestion import ManagedEmailIngestionRuntime
from channels.email.imap_source import ImapEmailSource
from channels.email.delivery_runtime import (
    create_default_email_delivery_worker,
    run_delivery_batch,
)


def _customer_hybrid_retriever(config, authoritative_store, *, readonly_indexes=False):
    """Build isolated customer indexes; no external embedding or network calls."""
    index_root = os.path.join(config.workspace, "workspace", "customer_memory")
    keyword = KeywordMemoryIndex(
        os.path.join(index_root, "customer_keyword_index.db"),
        readonly=readonly_indexes,
    )
    vector = VectorMemoryIndex(
        os.path.join(index_root, "customer_vector_index.db"),
        LocalHashEmbeddingAdapter(config.memory_embedding_dimensions),
        readonly=readonly_indexes,
    )
    return HybridMemoryRetriever(
        authoritative_store, keyword, vector,
        keyword_weight=config.memory_hybrid_keyword_weight,
        vector_weight=config.memory_hybrid_vector_weight,
        score_threshold=config.memory_hybrid_score_threshold,
    )


def _workspace_embedding(config):
    if config.workspace_memory_embedding_backend == "openai_compatible":
        return OpenAICompatibleEmbeddingAdapter(
            base_url=config.workspace_memory_embedding_base_url,
            api_key=config.workspace_memory_embedding_api_key,
            model=config.workspace_memory_embedding_model,
            dimensions=config.workspace_memory_embedding_dimensions,
            transfer_approved=config.workspace_memory_external_transfer_approved,
        )
    return LocalHashEmbeddingAdapter(config.workspace_memory_embedding_dimensions)


def _workspace_memory_runtime(config, provider):
    root = os.path.join(config.workspace, "workspace", "memory")
    store = WorkspaceSQLiteMemoryStore(
        os.path.join(root, "workspace_memory.db"),
        indexing_enabled=config.workspace_memory_hybrid_enabled,
    )
    service = WorkspaceMemoryService(store)
    actor = ActorContext(
        "workspace_operator", config.workspace_operator_id or "local",
        config.workspace_operator_tenant_id or "default",
        frozenset({"workspace_memory_reader", "workspace_memory_writer"}), True,
    )
    scope = MemoryScope(
        "workspace_private", actor.tenant_id, subject_id=actor.actor_id,
        project_id=stable_project_id(config.workspace), purpose="project_assistance",
    )
    retriever = DecayAwareRetriever(
        store,
        semantic_days=config.workspace_memory_semantic_half_life_days,
        episodic_days=config.workspace_memory_episodic_half_life_days,
        procedural_days=config.workspace_memory_procedural_half_life_days,
    )
    keyword = vector = worker = None
    if config.workspace_memory_hybrid_enabled:
        keyword = KeywordMemoryIndex(os.path.join(root, "workspace_keyword_index.db"))
        vector = VectorMemoryIndex(
            os.path.join(root, "workspace_vector_index.db"), _workspace_embedding(config),
        )
        hybrid = HybridMemoryRetriever(
            store, keyword, vector,
            keyword_weight=config.memory_hybrid_keyword_weight,
            vector_weight=config.memory_hybrid_vector_weight,
            score_threshold=config.memory_hybrid_score_threshold,
        )
        retriever = DecayAwareRetriever(
            hybrid,
            semantic_days=config.workspace_memory_semantic_half_life_days,
            episodic_days=config.workspace_memory_episodic_half_life_days,
            procedural_days=config.workspace_memory_procedural_half_life_days,
        )
        worker = MemoryIndexWorker(store, keyword, vector)
    review = WorkspaceMemoryReviewService(
        store, provider if config.workspace_memory_review_enabled else None,
        model=config.model,
    )
    return {
        "store": store, "service": service, "actor": actor, "scope": scope,
        "retriever": retriever, "keyword": keyword, "vector": vector,
        "worker": worker, "review": review,
    }


def _customer_path(
    workspace: str,
    configured_path: str,
    *,
    forbidden: tuple[str, ...] = (),
    required_parent: str | None = None,
) -> str:
    """Resolve a customer-only path inside the workspace and reject shared internals."""
    root = Path(workspace).resolve()
    candidate = Path(configured_path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("customer_path_outside_workspace") from exc
    forbidden_paths = {
        (Path(item) if Path(item).is_absolute() else root / item).resolve()
        for item in forbidden
    }
    if resolved in forbidden_paths:
        raise RuntimeError("customer_path_conflicts_with_internal_data")
    if required_parent:
        allowed_root = (root / required_parent).resolve()
        try:
            resolved.relative_to(allowed_root)
        except ValueError as exc:
            raise RuntimeError("customer_path_outside_dedicated_area") from exc
    return str(resolved)


def build_workspace_peer_agent(config, session_key: str) -> AgentLoop:
    """Build a same-level workspace analysis agent with read-only internal tools."""
    if not session_key.startswith("workspace_peer:"):
        raise ValueError("workspace peer requires a workspace_peer session key")
    if not config.api_key:
        raise RuntimeError("workspace_peer_provider_not_configured")
    provider = OpenAICompatProvider(
        api_key=config.api_key, base_url=config.base_url, model=config.model,
    )
    tools = ToolRegistry()
    tools.register(WorkspaceKnowledgeAnalysisTool())
    tools.register(WorkspaceProductAnalysisTool())
    session_manager = SessionManager(
        sessions_dir=os.path.join(config.workspace, "workspace", "peer_sessions")
    )
    context = ContextBuilder(
        workspace=config.workspace,
        identity_file=config.identity_file,
        skills_summary="",
        include_memory=True,
        include_workspace_details=False,
    )
    return AgentLoop(
        provider=provider,
        tools=tools,
        context=context,
        session_manager=session_manager,
        model=config.model,
        max_iterations=min(config.max_iterations, 6),
        session_key=session_key,
        workflow_service=None,
    )


def build_customer_agent(config, session_key: str, peer_coordinator=None) -> CustomerAgent:
    """Build the public agent with only filtered knowledge/catalog reads."""
    if not session_key.startswith("customer_portal:"):
        raise ValueError("customer agent requires a customer_portal session key")
    if not config.api_key:
        raise RuntimeError("customer_agent_provider_not_configured")

    identity_path = _customer_path(
        config.workspace,
        config.customer_identity_file,
        forbidden=(config.identity_file,),
    )
    if not os.path.isfile(identity_path):
        raise RuntimeError("customer_identity_not_configured")

    session_dir = _customer_path(
        config.workspace,
        getattr(config, "customer_session_dir", "workspace/customer_sessions"),
        forbidden=("workspace/sessions",),
    )
    memory_file = _customer_path(
        config.workspace,
        getattr(config, "customer_memory_file", "workspace/customer_memory/PUBLIC_MEMORY.md"),
        forbidden=("workspace/memory/MEMORY.md",),
        required_parent="workspace/customer_memory",
    )
    memory_enabled = bool(getattr(config, "customer_memory_enabled", True))

    provider = OpenAICompatProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )
    public_memory_store = None
    if getattr(config, "customer_long_term_memory_enabled", False):
        public_memory_store = PublicApprovedMemoryStore(
            os.path.join(config.workspace, "workspace", "customer_memory", "public_memory.db")
        )
        public_memory_store.import_approved_markdown(memory_file)
    tools = ToolRegistry()
    tools.register(CustomerPublicKnowledgeTool(public_memory_store=public_memory_store))
    tools.register(CustomerPublicCatalogTool())
    customer_m2_enabled = bool(
        getattr(config, "customer_conversation_memory_enabled", False)
        and session_key.startswith("customer_portal:account:")
    )
    if customer_m2_enabled:
        session_manager = BoundedSessionManager(
            sessions_dir=session_dir,
            max_turns=config.memory_max_turns,
            summarizer=ProviderTurnSummarizer(provider, model=config.model),
        )
        customer_working_store = CustomerWorkingMemoryStore(
            os.path.join(config.workspace, config.customer_data_database_path)
        )
        customer_long_term_store = None
        if getattr(config, "customer_long_term_memory_enabled", False):
            customer_main_store = CustomerSQLiteMemoryStore(
                os.path.join(config.workspace, config.customer_data_database_path),
                indexing_enabled=getattr(config, "memory_hybrid_retrieval_enabled", False),
            )
            customer_long_term_store = (
                _customer_hybrid_retriever(
                    config, customer_main_store, readonly_indexes=True,
                )
                if getattr(config, "memory_hybrid_retrieval_enabled", False)
                else customer_main_store
            )
        memory_lifecycle = BoundedWorkingMemoryLifecycle(
            session_manager, customer_store=customer_working_store,
            long_term_store=customer_long_term_store,
            recall_top_k=config.memory_recall_top_k,
        )
        memory_context_provider = StructuredWorkingMemoryContextProvider()

        def memory_request_factory(key, request_id, message, history, trusted):
            account_id = str(trusted.get("account_id") or "")
            tenant_id = str(trusted.get("tenant_id") or "")
            conversation_id = str(trusted.get("conversation_id") or "")
            return TurnRequest(
                request_id, ActorContext(
                    "customer", account_id, tenant_id, frozenset(), True,
                ),
                MemoryScope(
                    "customer_conversation", tenant_id, account_id=account_id,
                    conversation_id=conversation_id, purpose="customer_support",
                ),
                message, history, key,
            )
    else:
        session_manager = SessionManager(sessions_dir=session_dir)
        memory_lifecycle = None
        memory_context_provider = None
        memory_request_factory = None
    context = ContextBuilder(
        workspace=config.workspace,
        identity_file=os.path.relpath(identity_path, Path(config.workspace).resolve()),
        skills_summary="",
        include_memory=memory_enabled and not customer_m2_enabled,
        include_workspace_details=False,
        memory_file=memory_file,
        memory_management_guidance=False,
        memory_context_provider=memory_context_provider,
    )
    agent = CustomerAgent(
        provider=provider,
        tools=tools,
        context=context,
        session_manager=session_manager,
        model=config.model,
        max_iterations=min(config.max_iterations, 4),
        session_key=session_key,
        workflow_service=None,
        peer_coordinator=peer_coordinator,
        memory_lifecycle=memory_lifecycle,
        memory_request_factory=memory_request_factory,
    )
    print(f"[{session_key}] 客户 Agent 已隔离启动：仅公开知识/产品只读工具，无 MCP、Skill 和内部记忆")
    return agent


def build_agent(config, session_key: str, mcp_manager: MCPClientManager = None,
                workflow_service: WorkflowService | None = None,
                workspace_peer: WorkspacePeerCoordinator | None = None,
                customer_memory_reader: CustomerMemoryReader | None = None,
                workspace_actor: ActorContext | None = None,
                workspace_runtime: dict | None = None) -> AgentLoop:
    """
    组装 Agent 实例
    Args:
        config: 配置对象（由 load_config() 返回）
        session_key: 会话标识，格式为 "{channel}:{sender_id}"
        mcp_manager: MCP 客户端管理器（可选）
    加载配置、创建各组件并组装 AgentLoop：
    1. 加载配置文件
    2. 验证 API 密钥
    3. 创建 LLM Provider
    4. 注册文件系统工具
    5. 注册 MCP 工具（如果有）
    6. 创建上下文构建器
    7. 组装 AgentLoop

    Returns:
        AgentLoop: 组装完成的 Agent 实例
    """
    if session_key.startswith("customer_portal:"):
        return build_customer_agent(config, session_key, workspace_peer)

    # 验证 API 密钥
    if not config.api_key:
        print("错误: 未配置 API 密钥")
        print("请设置环境变量 NANOCLAW_API_KEY")
        sys.exit(1)

    # 创建 LLM Provider
    provider = OpenAICompatProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )

    # 创建工具注册表并注册文件系统工具
    tools = ToolRegistry()
    tools.register(ReadFileTool(config.workspace))
    tools.register(WriteFileTool(config.workspace))
    tools.register(ListDirTool(config.workspace))
    tools.register(ExecTool(config.workspace))
    tools.register(WebSearchTool())
    tools.register(WebFetchTool())
    tools.register(QueryInboundEmailTool())

    if getattr(config, "workspace_customer_memory_read_enabled", False):
        if workspace_actor is None:
            operator_id = str(getattr(config, "workspace_operator_id", "")).strip()
            tenant_id = str(getattr(config, "workspace_operator_tenant_id", "")).strip()
            if not operator_id or not tenant_id:
                raise RuntimeError("workspace_operator_identity_not_configured")
            workspace_actor = ActorContext(
                "workspace_operator", operator_id, tenant_id,
                frozenset({"customer_memory_reader"}), True,
            )
        if customer_memory_reader is None:
            readonly_store = ReadonlyCustomerMemoryStore(
                os.path.join(config.workspace, config.customer_data_database_path)
            )
            customer_memory_reader = CustomerMemoryReader(
                readonly_store,
                CustomerMemoryAccessAudit(os.path.join(
                    config.workspace, "workspace", "memory",
                    "customer_memory_access_audit.db",
                )),
                max_top_k=min(5, config.memory_recall_top_k),
                search_backend=(
                    _customer_hybrid_retriever(
                        config, readonly_store, readonly_indexes=True,
                    )
                    if getattr(config, "memory_hybrid_retrieval_enabled", False)
                    else None
                ),
            )
        tools.register(ReadCustomerMemoryTool(customer_memory_reader, workspace_actor))

        # 注册子 Agent 工具
    def create_provider(model=None):
        """Provider 工厂函数，子 Agent 用。"""
        # 子 Agent 默认用 subagent 模型（更便宜）
        if model is None and config.models:
            model = config.models.get("subagent", config.model)
        return OpenAICompatProvider(
            api_key=config.api_key,
            base_url=config.base_url,
            model=model or config.model
        )

    tools.register(SpawnSubagentTool(
        provider_factory=create_provider,
        tools_registry=tools,
        workspace=config.workspace,
    ))

    # 注册 MCP 工具（如果有）
    if mcp_manager:
        mcp_tools = mcp_manager.get_tools()
        for mcp_tool in mcp_tools:
            tools.register(mcp_tool)
        print(f"[{session_key}] 已注册 MCP 工具: {len(mcp_tools)} 个")

    # 加载技能摘要
    skills_loader = SkillsLoader(
        skills_dir=os.path.join(config.workspace, "skills")
    )
    skills_summary = skills_loader.build_skills_summary()

    # 如果发现技能，打印数量
    if skills_summary:
        skills_list = skills_loader.list_skills()
        print(f"{session_key} 已发现技能: {len(skills_list)} 个")

    # 创建会话管理器
    workspace_m2_enabled = bool(getattr(config, "workspace_memory_enabled", False))
    memory_command_router = None
    if workspace_m2_enabled:
        session_manager = BoundedSessionManager(
            sessions_dir=os.path.join(config.workspace, "workspace", "sessions"),
            max_turns=config.memory_max_turns,
            summarizer=ProviderTurnSummarizer(provider, model=config.model),
        )
        workspace_working_store = WorkspaceWorkingMemoryStore(
            os.path.join(config.workspace, "workspace", "memory", "workspace_memory.db")
        )
        workspace_runtime = workspace_runtime or _workspace_memory_runtime(config, provider)
        memory_lifecycle = WorkspaceMemoryLifecycle(
            session_manager, workspace_working_store,
            workspace_runtime["service"], workspace_runtime["retriever"],
            extractor=WorkspaceMemoryExtractor(provider, model=config.model),
            recall_top_k=config.memory_recall_top_k,
            auto_extract=config.workspace_memory_auto_extract_enabled,
        )
        memory_context_provider = StructuredWorkingMemoryContextProvider()
        memory_command_router = WorkspaceMemoryCommandRouter(
            workspace_runtime["service"], workspace_runtime["actor"],
            workspace_runtime["scope"], review_service=workspace_runtime["review"],
        )

        def memory_request_factory(key, request_id, message, history, trusted):
            base_scope = workspace_runtime["scope"]
            return TurnRequest(
                request_id, workspace_runtime["actor"],
                MemoryScope(
                    "workspace_private", base_scope.tenant_id,
                    subject_id=base_scope.subject_id,
                    project_id=base_scope.project_id, conversation_id=key,
                    purpose="project_assistance",
                ),
                message, history, key,
            )
    else:
        session_manager = SessionManager(
            sessions_dir=os.path.join(config.workspace, "workspace", "sessions")
        )
        memory_lifecycle = None
        memory_context_provider = None
        memory_request_factory = None

    # session_key = "cli:direct"
    # 如果有历史，显示恢复提示
    existing_history = session_manager.get_history(session_key)
    if existing_history:
        print(f"[{session_key}] 已恢复 {len(existing_history)} 条历史消息")

    # 打印已注册的工具
    print(f"[{session_key}] 已注册工具：{tools.list_tools()}")

    # 创建上下文构建器
    context = ContextBuilder(
        workspace=config.workspace,
        identity_file=config.identity_file,
        skills_summary=skills_summary,
        include_memory=(not workspace_m2_enabled
                        or config.workspace_memory_legacy_fallback_enabled),
        memory_management_guidance=not workspace_m2_enabled,
        include_workspace_details=True,
        memory_context_provider=memory_context_provider,
    )

    # 创建 Token 压缩器
    consolidator = MemoryConsolidator(
        provider=provider,
        workspace=config.workspace,
        token_budget=16000
    )

    # 组装 AgentLoop
    agent = AgentLoop(
        provider=provider,
        tools=tools,
        context=context,
        session_manager=session_manager,
        model=config.model,
        max_iterations=config.max_iterations,
        # session_key="cli:direct",
        session_key=session_key,
        workflow_service=workflow_service,
        memory_lifecycle=memory_lifecycle,
        memory_request_factory=memory_request_factory,
        memory_command_router=memory_command_router,
    )

    agent.consolidator = None if workspace_m2_enabled else consolidator

    return agent


async def interactive_loop(agent: AgentLoop) -> None:
    """
    交互式对话循环

    读取用户输入，调用 Agent 处理并打印响应。
    支持命令：/exit、/clear、/tools

    Args:
        agent: AgentLoop 实例
    """
    print("\n开始对话（输入 /exit 退出）")

    while True:
        try:
            # 读取用户输入
            user_input = input("\n你: ").strip()

            # 空输入跳过
            if not user_input:
                continue

            # 处理命令
            if user_input == "/exit":
                print("再见！")
                break

            if user_input == "/clear":
                agent.clear_history()
                print("对话历史已清空")
                continue

            if user_input == "/tools":
                tools = agent.tools.list_tools()
                print(f"可用工具: {', '.join(tools)}")
                continue

            # 调用 Agent 处理
            print("\nNanoClaw: ", end="", flush=True)
            response = await agent.run(user_input)
            print(response)

        except KeyboardInterrupt:
            # Ctrl+C 优雅退出
            print("\n\n再见！")
            break

        except EOFError:
            # 输入结束（如管道输入完毕）
            print("\n再见！")
            break


# 新版：通过 Gateway 启动
async def async_main() -> None:
    """
    异步主入口

    启动 MCP Server、组装 Agent、启动 Gateway。
    """
    configure_privacy_logging()
    # 启动 banner
    print("=" * 50)
    print("  NanoClaw - 智能代码助手")
    print("  模型: Kimi-K2.5 (硅基流动)")
    print("=" * 50)
    #加载配置文件
    config = load_config()
    bus = MessageBus()
    workflow_service = WorkflowService(os.path.join(config.workspace, "workspace", "workflows"))
    task_runtime_repository = None
    task_runtime_service = None
    task_runtime_worker = None
    task_runtime_stop = None
    task_runtime_task = None
    workspace_task_owner = None
    if config.task_runtime_enabled:
        task_runtime_repository = create_task_runtime_repository()
        task_runtime_service = TaskRuntimeService(
            task_runtime_repository,
            artifact_root=os.path.join(config.workspace, "outputs", "task-runtime"),
        )
        workspace_task_owner = TaskOwner(
            config.workspace_operator_tenant_id or "default",
            "workspace", config.workspace_operator_id or "local-operator",
        )
        task_runtime_worker = TaskRuntimeWorker(task_runtime_service)
        task_runtime_stop = asyncio.Event()
        task_runtime_task = asyncio.create_task(task_runtime_worker.run(task_runtime_stop))
    workspace_runtime = None
    workspace_memory_router = None
    if config.workspace_memory_enabled:
        workspace_memory_provider = OpenAICompatProvider(
            api_key=config.api_key, base_url=config.base_url, model=config.model,
        )
        workspace_runtime = _workspace_memory_runtime(config, workspace_memory_provider)
        workspace_memory_router = create_workspace_memory_router(
            workspace_runtime["service"], workspace_runtime["actor"],
            workspace_runtime["scope"], review_service=workspace_runtime["review"],
            token=config.workspace_memory_admin_token,
            allowed_origins=config.workspace_memory_admin_allowed_origins,
        )

    # 启动 MCP Server（如果有配置）
    mcp_manager = None
    if config.mcp_servers:
        mcp_manager = MCPClientManager(config.mcp_servers)
        await mcp_manager.connect_all(timeout=30)
        # await mcp_manager.connect_all(timeout=30.0)

    # 定义 Agent 工厂函数
    workspace_customer_reader = None
    workspace_reader_actor = None
    workspace_peer = WorkspacePeerCoordinator(
        lambda peer_session_key: build_workspace_peer_agent(config, peer_session_key)
    )

    def create_agent(session_key: str) -> AgentLoop:
        return build_agent(
            config, session_key, mcp_manager, workflow_service, workspace_peer,
            workspace_customer_reader, workspace_reader_actor, workspace_runtime,
        )

    # 注册渠道
    cli_channel = CLIChannel(bus)
    channels = [cli_channel]

    # 飞书渠道（如果配置了 feishu app_id / app_secret 就自动启用）
    if config.feishu_app_id and config.feishu_app_secret:
        feishu_channel = FeishuChannel(bus, config.feishu_app_id, config.feishu_app_secret)
        channels.append(feishu_channel)
        print("[启动] 已启用飞书渠道")
    else:
        print("[启动] 未配置飞书，跳过飞书渠道")

    # QQ 渠道（如果配置了 qq app_id / app_secret 就自动启用）
    if config.qq_app_id and config.qq_app_secret:
        qq_channel = QQChannel(bus, config.qq_app_id, config.qq_app_secret)
        channels.append(qq_channel)
        print("[启动] 已启用 QQ 渠道")
    else:
        print("[启动] 未配置 QQ，跳过 QQ 渠道")

    # Web 渠道（默认启用，不需要外部凭证）
    shared_knowledge_repository = KnowledgeRepository()
    shared_knowledge_pipeline = RagPipeline()
    shared_pdf_ingestion = PdfIngestionCoordinator(
        shared_knowledge_repository, shared_knowledge_pipeline,
    )
    shared_conversations = conversation_backend() == "mysql"
    workspace_conversations = (
        MySQLWorkspaceConversationService(
            os.path.join(config.workspace, "workspace", "sessions"))
        if shared_conversations else ConversationService(
            os.path.join(config.workspace, "workspace", "sessions"))
    )
    if config.web_enabled:
        web_channel = WebChannel(bus, host=config.web_host, port=config.web_port,
                                 workflow_service=workflow_service,
                                 conversation_service=workspace_conversations,
                                 workspace_memory_router=workspace_memory_router,
                                 knowledge_repository=shared_knowledge_repository,
                                 knowledge_pipeline=shared_knowledge_pipeline,
                                 pdf_ingestion=shared_pdf_ingestion,
                                 root_page="landing")
        channels.append(web_channel)
        print(f"[启动] 已启用 Web 渠道: http://{config.web_host}:{config.web_port}")
    else:
        print("[启动] Web 渠道已禁用")

    if config.workspace_web_enabled:
        dedicated_workspace = WebChannel(
            bus, host=config.workspace_web_host, port=config.workspace_web_port,
            workflow_service=workflow_service,
            conversation_service=workspace_conversations,
            workspace_memory_router=workspace_memory_router,
            knowledge_repository=shared_knowledge_repository,
            knowledge_pipeline=shared_knowledge_pipeline,
            pdf_ingestion=shared_pdf_ingestion,
            task_runtime_service=task_runtime_service,
            workspace_task_owner=workspace_task_owner,
            channel_name="workspace_web", root_page="workspace",
        )
        channels.append(dedicated_workspace)
        print(
            f"[启动] 已启用独立工作空间: "
            f"http://{config.workspace_web_host}:{config.workspace_web_port}"
        )

    customer_memory_service = None
    if config.customer_portal_enabled:
        customer_identity_service = None
        customer_conversation_repository = None
        if config.customer_auth_enabled:
            session_secret = load_or_create_customer_session_secret(
                config.workspace, os.environ.get("NANOCLAW_CUSTOMER_SESSION_SECRET", ""),
            )
            customer_identity_repository = CustomerIdentityRepository(
                os.path.join(config.workspace, config.customer_auth_database_path)
            )
            customer_identity_service = CustomerIdentityService(
                customer_identity_repository,
                Argon2PasswordHasher(),
                tenant_id=config.workspace_operator_tenant_id or "default",
                registration_enabled=config.customer_registration_enabled,
                idle_minutes=config.customer_auth_idle_minutes,
                absolute_hours=config.customer_auth_absolute_hours,
            )
            customer_conversation_repository = (
                MySQLCustomerConversationRepository(
                    cursor_secret=session_secret.encode("utf-8"))
                if shared_conversations else CustomerConversationRepository(
                    os.path.join(config.workspace, config.customer_data_database_path),
                    cursor_secret=session_secret.encode("utf-8"),
                )
            )
            if config.customer_long_term_memory_enabled:
                customer_memory_service = CustomerMemoryService(
                    CustomerSQLiteMemoryStore(
                        os.path.join(config.workspace, config.customer_data_database_path),
                        indexing_enabled=config.memory_hybrid_retrieval_enabled,
                    )
                )
                public_store = PublicApprovedMemoryStore(
                    os.path.join(config.workspace, "workspace", "customer_memory", "public_memory.db")
                )
                public_store.import_approved_markdown(
                    os.path.join(config.workspace, config.customer_memory_file)
                )
        customer_portal_channel = CustomerPortalChannel(
            bus, host=config.customer_portal_host, port=config.customer_portal_port,
            identity_service=customer_identity_service,
            conversation_repository=customer_conversation_repository,
            memory_service=customer_memory_service,
            task_runtime_service=task_runtime_service,
        )
        channels.append(customer_portal_channel)
        print(
            f"[启动] 已启用独立客户门户: "
            f"http://{config.customer_portal_host}:{config.customer_portal_port}"
        )
    else:
        print("[启动] 独立客户门户已禁用")

    memory_index_task = None
    workspace_memory_task = None
    if config.memory_hybrid_retrieval_enabled:
        index_authority = (
            customer_memory_service.store if customer_memory_service is not None
            else CustomerSQLiteMemoryStore(
                os.path.join(config.workspace, config.customer_data_database_path),
                indexing_enabled=True,
            )
        )
        index_authority.enqueue_active_for_indexing()
        index_retriever = _customer_hybrid_retriever(config, index_authority)
        index_worker = MemoryIndexWorker(
            index_authority, index_retriever.keyword_index, index_retriever.vector_index,
        )
        await asyncio.to_thread(index_worker.drain, 1000)

        async def run_memory_index_worker() -> None:
            while True:
                processed = await asyncio.to_thread(index_worker.drain, 100)
                await asyncio.sleep(0 if processed else 1)

        memory_index_task = asyncio.create_task(run_memory_index_worker())

    if workspace_runtime is not None:
        workspace_runtime["store"].enqueue_active_for_indexing()
        if workspace_runtime["worker"] is not None:
            await asyncio.to_thread(workspace_runtime["worker"].drain, 1000)

        async def run_workspace_memory_maintenance() -> None:
            loop = asyncio.get_running_loop()
            next_review = loop.time()
            while True:
                try:
                    if workspace_runtime["worker"] is not None:
                        await asyncio.to_thread(workspace_runtime["worker"].drain, 100)
                    if config.workspace_memory_governance_enabled:
                        await asyncio.to_thread(workspace_runtime["store"].expire_due)
                    if config.workspace_memory_review_enabled and loop.time() >= next_review:
                        await workspace_runtime["review"].run_once(
                            workspace_runtime["actor"], workspace_runtime["scope"],
                        )
                        next_review = loop.time() + config.workspace_memory_review_interval_hours * 3600
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[WorkspaceMemory] 维护失败: {type(exc).__name__}")
                await asyncio.sleep(config.workspace_memory_maintenance_seconds)

        workspace_memory_task = asyncio.create_task(run_workspace_memory_maintenance())

    if config.workspace_customer_memory_read_enabled:
        workspace_reader_actor = ActorContext(
            "workspace_operator", config.workspace_operator_id,
            config.workspace_operator_tenant_id,
            frozenset({"customer_memory_reader"}), True,
        )
        readonly_store = ReadonlyCustomerMemoryStore(os.path.join(
            config.workspace, config.customer_data_database_path,
        ))
        workspace_customer_reader = CustomerMemoryReader(
            readonly_store,
            CustomerMemoryAccessAudit(os.path.join(
                config.workspace, "workspace", "memory",
                "customer_memory_access_audit.db",
            )),
            max_top_k=min(5, config.memory_recall_top_k),
            search_backend=(
                _customer_hybrid_retriever(
                    config, readonly_store, readonly_indexes=True,
                )
                if config.memory_hybrid_retrieval_enabled else None
            ),
        )

    # 启动网关
    gateway = Gateway(bus, channels, create_agent,
                      coordinator=coordinator_from_environment())

    # Email RFQ notifications are durable DB outbox messages, not Agent replies.
    email_config = load_email_config()
    notification_task = None
    smtp_delivery_task = None
    managed_ingestion_task = None
    if email_config.qq_notify_enabled:
        email_config.validate_qq_notification()
        if not (config.qq_app_id and config.qq_app_secret):
            raise ValueError("QQ notification requires NANOCLAW_QQ_APP_ID and NANOCLAW_QQ_APP_SECRET")
        repository = EmailRepository()
        dispatcher = QQNotificationDispatcher(repository, qq_channel.send)
        source = ImapEmailSource(email_config.imap_settings(), email_config.limits())
        ingestion = EmailIngestionService(repository, qq_target_id=email_config.qq_target_id,
                                          qq_target_type=email_config.qq_target_type,
                                          extraction_timeout_seconds=email_config.extraction_timeout_seconds)

        async def run_email_ingestion_and_notifications() -> None:
            while True:
                try:
                    await ingestion.poll_once(source, email_config.account_id, email_config.folder)
                except Exception as exc:
                    error_code = getattr(exc, "code", type(exc).__name__)
                    print(f"[EmailWorker] 拉取失败: {error_code}")
                await dispatcher.dispatch_pending()
                await asyncio.sleep(email_config.poll_seconds)

        notification_task = asyncio.create_task(run_email_ingestion_and_notifications())

    if email_config.managed_accounts_enabled:
        managed_ingestion = ManagedEmailIngestionRuntime(email_config)

        async def run_managed_email_ingestion() -> None:
            while True:
                results = await managed_ingestion.poll_due()
                for result in results:
                    if result["status"] == "error":
                        print(f"[ManagedEmail] 账户轮询失败: {result['error_code']}")
                        continue
                    messages = result["messages"]
                    accepted = sum(item.get("status") == "needs_review" for item in messages)
                    ignored = sum(item.get("status") == "ignored_non_trade" for item in messages)
                    if accepted or ignored:
                        print(f"[ManagedEmail] 外贸询盘待审核={accepted} 非外贸跳过={ignored}")
                await asyncio.sleep(max(1, min(email_config.managed_scan_seconds, 60)))

        managed_ingestion_task = asyncio.create_task(run_managed_email_ingestion())
        mode = "remote_llm" if email_config.remote_extraction_approved else "deterministic_local"
        print(f"[启动] 已启用工作区邮箱自动外贸询盘解析: {mode}")
    else:
        print("[启动] 工作区邮箱自动外贸询盘解析未启用")

    if config.email_smtp_worker_enabled:
        smtp_worker = create_default_email_delivery_worker(
            worker_id=f"smtp-{uuid.uuid4()}",
            timeout_seconds=config.email_smtp_timeout_seconds,
        )

        async def run_real_smtp_delivery() -> None:
            recovered = await asyncio.to_thread(smtp_worker.repository.requeue_expired_leases)
            if recovered:
                print(f"[SMTP Worker] 已恢复过期租约: {recovered}")
            while True:
                try:
                    results = await asyncio.to_thread(
                        run_delivery_batch, smtp_worker, config.email_smtp_batch_size
                    )
                    for result in results:
                        print(
                            f"[SMTP Worker] 投递状态: {result['status']} "
                            f"delivery_id={result['delivery_id']}"
                        )
                    if not results:
                        await asyncio.sleep(config.email_smtp_poll_seconds)
                    else:
                        await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_code = getattr(exc, "code", type(exc).__name__)
                    print(f"[SMTP Worker] 处理失败: {error_code}")
                    await asyncio.sleep(config.email_smtp_poll_seconds)

        smtp_delivery_task = asyncio.create_task(run_real_smtp_delivery())
        print("[启动] 已启用真实 SMTP_SSL 投递 Worker")
    else:
        print("[启动] 真实 SMTP 投递 Worker 未启用")

    # 预创建 CLI Agent，让初始化信息在启动时就显示
    cli_session_key = "cli:local"
    agent = create_agent(cli_session_key)
    gateway._agents[cli_session_key] = agent

    # 注入工具列表和清空回调给 CLI 渠道
    cli_channel.tool_names = agent.tools.list_tools()
    cli_channel._clear_callback = lambda: agent.clear_history()

    try:
        await gateway.run()
    except KeyboardInterrupt:
        print("\n[NanoClaw] 正在退出...")
    finally:
        if notification_task:
            notification_task.cancel()
            await asyncio.gather(notification_task, return_exceptions=True)
        if smtp_delivery_task:
            smtp_delivery_task.cancel()
            await asyncio.gather(smtp_delivery_task, return_exceptions=True)
        if managed_ingestion_task:
            managed_ingestion_task.cancel()
            await asyncio.gather(managed_ingestion_task, return_exceptions=True)
        if memory_index_task:
            memory_index_task.cancel()
            await asyncio.gather(memory_index_task, return_exceptions=True)
        if workspace_memory_task:
            workspace_memory_task.cancel()
            await asyncio.gather(workspace_memory_task, return_exceptions=True)
        if task_runtime_stop is not None:
            task_runtime_stop.set()
        if task_runtime_task is not None:
            await asyncio.gather(task_runtime_task, return_exceptions=True)
        if task_runtime_repository is not None:
            task_runtime_repository.close()
        # 清理 MCP 连接
        if mcp_manager:
            await mcp_manager.shutdown()


def main() -> None:
    """
    主入口（同步包装）

    打印启动 banner，组装 Agent 并启动交互循环。
    """
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n[NanoClaw] 正在退出...")
        import os
        os._exit(0)  # 强制退出，杀掉残留的 WebSocket 子线程


if __name__ == "__main__":
    main()
