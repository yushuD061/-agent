"""
NanoClaw 外贸业务 MCP Server

通过 FastMCP 将所有业务工具注册为 MCP 工具，
NanoClaw 通过 MCP Client 连接后即可调用。

启动方式：
    python -m mcp_servers.foreign_trade_inquiry_server

P0：不修改 nanoclaw 源码，以独立 MCP Server 运行
P1：支持 Manager 面板的 MCP 开关控制

工作流顺序（由 LLM 按 instructions 遵守，不设硬状态机）：
    extract_rfq → search_product_catalog → calculate_quote
    → create_quote_version → approve_outbound_message → create_followup_task
"""

import json
import os
import sys

_PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from mcp.server.fastmcp import FastMCP

from agent.business.config import load_business_config
from agent.business.database import init_db, close_all

INSTRUCTIONS = """# 外贸询盘报价工作流

你是外贸报价助手，严格按以下顺序处理询盘：

## 步骤 1 — 字段抽取
调用 extract_rfq 解析询盘邮件/消息，提取客户、产品、数量等信息。
缺失字段标记为"待确认"，不要自行补全。

## 步骤 2 — 产品匹配
调用 search_product_catalog 在产品库中搜索匹配的 SKU。
如果提取结果中有明确的 SKU 则精确查询；否则用产品描述关键词搜索。

## 步骤 3 — 报价计算
调用 calculate_quote 进行确定性报价计算（不含 LLM，100% 准确）。
传入合适的数量、贸易条款、加价率等参数。

## 步骤 4 — 生成报价单
调用 create_quote_version 创建询盘对应的报价单。

## 步骤 5 — 提交审批
调用 approve_outbound_message(action="request") 提交审批。
审批通过前不得外发。

## 步骤 6 — 跟进任务
调用 create_followup_task 创建跟进提醒。

## 规则
- 金额计算全部用 calculate_quote，不能用 LLM 算
- 缺失信息标注为待确认，不自行补全
- 外发前必须经过审批
"""

mcp = FastMCP("quote-business", instructions=INSTRUCTIONS + """

## 外贸增长工作台
潜客与开发业务使用 trade_workbench_* 工具。每个阶段必须返回结果、证据、
风险、待补充和下一阶段输入。研究/评分可在内部继续；邮件与报价只能产生待审
草稿。批准邮件后仍需第二次独立排队，且排队不会启动 SMTP Worker。
""")

_trade_workbench_service = None


def _workbench():
    global _trade_workbench_service
    if _trade_workbench_service is None:
        init_db()
        from agent.business.trade_workbench_repository import create_trade_workbench_repository
        from agent.business.trade_workbench_service import TradeWorkbenchService
        _trade_workbench_service = TradeWorkbenchService(create_trade_workbench_repository())
    return _trade_workbench_service


@mcp.tool()
def query_trade_data(query_code: str = "", filters: str = "{}", list_contracts: bool = False) -> str:
    """执行批准的固定外贸运营分析查询；不接受或生成任意 SQL。

    可用 query_code 通过 list_contracts=true 获取。filters 必须是 JSON 对象，
    服务端执行字段 allowlist、参数化 SQL、50 行上限和审计哈希。
    当前数据源是本地/运营库演示数据，不是尚未部署的 trade_dw。
    """
    from agent.tools.query_trade_data import query_trade_data_impl
    return query_trade_data_impl(query_code, filters, list_contracts)


# ── 工具 1: 询盘字段抽取 ────────────────────────────────

@mcp.tool()
async def extract_rfq(session_key: str, raw_text: str) -> str:
    """从英文询盘邮件或消息中抽取结构化字段

    使用 LLM 解析客户名、国家、产品描述、数量、贸易条款等。
    缺失字段标记为"待确认"，不会自行补全。

    Args:
        session_key: 会话标识（由 NanoClaw 传入，格式 {channel}:{sender_id}）
        raw_text: 询盘原始文本（英文）
    """
    from agent.tools.extract_rfq import extract_rfq_impl
    return await extract_rfq_impl(session_key, raw_text)


# ── 工具 2: 产品库搜索 ──────────────────────────────────

@mcp.tool()
def search_product_catalog(keyword: str = "", category: str = "", sku: str = "", limit: int = 5) -> str:
    """在产品库中搜索匹配的产品

    支持关键词模糊匹配、分类过滤和精确 SKU 查询。
    返回候选产品列表及匹配依据。

    Args:
        keyword: 搜索关键词（在产品名、规格中匹配）
        category: 产品分类过滤（可选）
        sku: 精确 SKU 查询（可选）
        limit: 最多返回结果数，默认 5
    """
    from agent.tools.search_product import search_product_catalog_impl
    return search_product_catalog_impl(keyword=keyword, category=category, sku=sku, limit=limit)


# ── 工具 3: 报价计算 ────────────────────────────────────

@mcp.tool()
def calculate_quote(
    sku: str,
    quantity: int,
    unit_price_usd: float = 0.0,
    markup_percent: float = 0.0,
    delivery_term: str = "FOB",
    destination_country: str = "",
    packaging_cost_usd: float = 0.0,
    freight_cost_usd: float = 0.0,
    validity_days: int = 15,
    target_currency: str = "USD",
    discount_percent_override: float = 0.0,
) -> str:
    """确定性报价计算

    纯算法计算，不调用 LLM。
    自动应用批量折扣阶梯，支持汇率转换。

    Args:
        sku: 产品 SKU
        quantity: 订购数量
        unit_price_usd: 美元单价（不传则用产品库基准价）
        markup_percent: 加价百分比（不传则用配置默认值 15%）
        delivery_term: 贸易条款，默认 FOB
        destination_country: 目的国
        packaging_cost_usd: 包装费（美元），默认 0
        freight_cost_usd: 运费（美元），默认 0
        validity_days: 报价有效期天数，默认 15
        target_currency: 目标货币，默认 USD
        discount_percent_override: 覆盖折扣百分比（设为 0 则自动阶梯）
    """
    from agent.tools.calculate_quote import calculate_quote_impl
    return calculate_quote_impl(
        sku=sku, quantity=quantity,
        unit_price_usd=unit_price_usd if unit_price_usd > 0 else None,
        markup_percent=markup_percent if markup_percent > 0 else None,
        delivery_term=delivery_term, destination_country=destination_country,
        packaging_cost_usd=packaging_cost_usd, freight_cost_usd=freight_cost_usd,
        validity_days=validity_days or None,
        target_currency=target_currency,
        discount_percent_override=discount_percent_override or None,
    )


# ── 工具 4: 库存校验 ────────────────────────────────────

@mcp.tool()
def check_inventory(sku: str, quantity: int) -> str:
    """校验产品库存是否满足需求数量

    同时检查最小起订量 (MOQ) 约束。

    Args:
        sku: 产品 SKU
        quantity: 需求数量
    """
    from agent.tools.check_inventory import check_inventory_impl
    return check_inventory_impl(sku, quantity)


# ── 工具 5: 报价单版本 ──────────────────────────────────

@mcp.tool()
def create_quote_version(
    rfq_id: int,
    customer_name: str,
    customer_company: str,
    country: str,
    items: str,
    subtotal_usd: float = 0.0,
    discount_percent: float = 0.0,
    discount_amount: float = 0.0,
    packaging_cost_usd: float = 0.0,
    freight_cost_usd: float = 0.0,
    total_usd: float = 0.0,
    validity_days: int = 15,
    valid_until: str = "",
    payment_terms: str = "T/T",
    delivery_term: str = "FOB",
    remarks_cn: str = "",
    remarks_en: str = "",
    risk_notes: str = "",
    exchange_rate_note: str = "",
) -> str:
    """创建报价单版本

    基于询盘和报价计算结果创建正式报价单。
    items 参数为 JSON 数组字符串，每项含 sku/quantity/unit_price_usd 等。

    Args:
        rfq_id: 询盘 ID（extract_rfq 返回结果中的 rfq_id）
        customer_name: 客户名称
        customer_company: 客户公司
        country: 国家
        items: 报价项 JSON 数组字符串
        subtotal_usd: 小计金额
        discount_percent: 折扣百分比
        discount_amount: 折扣金额
        packaging_cost_usd: 包装费
        freight_cost_usd: 运费
        total_usd: 总计金额
        validity_days: 有效期天数
        valid_until: 有效期截止日期
        payment_terms: 支付条款
        delivery_term: 贸易条款
        remarks_cn: 中文备注
        remarks_en: 英文备注
        risk_notes: 风险提示
        exchange_rate_note: 汇率说明
    """
    from agent.tools.create_quote import create_quote_version_impl
    try:
        items_list = json.loads(items) if isinstance(items, str) else items
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": "items 参数必须是有效的 JSON 数组"}, ensure_ascii=False, indent=2)
    return create_quote_version_impl(
        rfq_id=rfq_id, customer_name=customer_name,
        customer_company=customer_company, country=country,
        items=items_list, subtotal_usd=subtotal_usd,
        discount_percent=discount_percent, discount_amount=discount_amount,
        packaging_cost_usd=packaging_cost_usd, freight_cost_usd=freight_cost_usd,
        total_usd=total_usd, validity_days=validity_days, valid_until=valid_until,
        payment_terms=payment_terms, delivery_term=delivery_term,
        remarks_cn=remarks_cn, remarks_en=remarks_en, risk_notes=risk_notes,
        exchange_rate_note=exchange_rate_note,
    )


# ── 工具 6: 跟进任务 ────────────────────────────────────

@mcp.tool()
def create_followup_task(quote_id: int, task_type: str = "follow_up", title: str = "", description: str = "", due_days: int = 3) -> str:
    """创建跟进任务

    为报价单创建跟进提醒任务。

    Args:
        quote_id: 报价单 ID
        task_type: 任务类型 (follow_up / approval_reminder / customer_reply)
        title: 任务标题（不传则自动生成）
        description: 任务描述
        due_days: 截止天数（从今天起）
    """
    from agent.tools.create_followup import create_followup_task_impl
    return create_followup_task_impl(quote_id, task_type, title, description, due_days)


# ── 工具 7: 审批外发 ────────────────────────────────────

@mcp.tool()
def approve_outbound_message(quote_id: int, action: str = "request", reviewer: str = "", comment: str = "", version: int = 1) -> str:
    """审批外发内容

    三步流程：
    1. request  — 提交审批申请
    2. approve  — 审批通过
    3. reject   — 驳回

    未审批的外发内容阻断率要求 100%。

    Args:
        quote_id: 报价单 ID
        action: 动作 (request / approve / reject)
        reviewer: 审批人姓名（approve/reject 时必填）
        comment: 审批意见
        version: 报价版本号
    """
    from agent.tools.approve_message import approve_outbound_message_impl
    return approve_outbound_message_impl(quote_id, action, reviewer, comment, version)


def trade_workbench_input_status() -> str:
    """检查外贸增长工作台现有输入，所有缺口明确返回“待补充”。"""
    return json.dumps(_workbench().input_status(), ensure_ascii=False, indent=2)


def trade_workbench_create_campaign(name: str) -> str:
    """创建内部外贸活动；不会搜索、联系客户或发送报价。"""
    return json.dumps(_workbench().create_campaign(name), ensure_ascii=False, indent=2)


def trade_workbench_get_campaign(campaign_id: str) -> str:
    """读取活动及各阶段最新的真实持久化结果。"""
    return json.dumps(_workbench().repository.get_campaign(campaign_id), ensure_ascii=False, indent=2)


def trade_workbench_run_stage(campaign_id: str, stage: str, payload: str = "{}") -> str:
    """运行一个内部阶段。payload 是 JSON 对象；邮件和报价阶段只生成待审草稿。"""
    try:
        values = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(values, dict):
            raise ValueError
        result = _workbench().run_stage(campaign_id, stage, values)
        from dataclasses import asdict
        return json.dumps(asdict(result), ensure_ascii=False, indent=2)
    except (ValueError, json.JSONDecodeError):
        return json.dumps({"error_code": "trade_stage_payload_invalid"}, ensure_ascii=False)


def trade_workbench_pause(campaign_id: str, if_match: str) -> str:
    """暂停活动；已持久化结果不会丢失，后续可从当前节点恢复。"""
    return json.dumps(_workbench().repository.pause(campaign_id, expected_etag=if_match), ensure_ascii=False, indent=2)


def trade_workbench_resume(campaign_id: str, if_match: str) -> str:
    """从已持久化的当前节点恢复活动。"""
    return json.dumps(_workbench().repository.resume(campaign_id, expected_etag=if_match), ensure_ascii=False, indent=2)


def trade_workbench_approve_email(draft_id: str, content_hash: str) -> str:
    """人工批准邮件草稿；本动作不会排队或发送。"""
    return json.dumps(_workbench().approve_outreach(draft_id, expected_hash=content_hash), ensure_ascii=False, indent=2)


def trade_workbench_queue_email(draft_id: str, content_hash: str, account_id: str, recipient: str) -> str:
    """对已批准草稿执行第二次人工排队；SMTP Worker 仍不会由本工具启动。"""
    return json.dumps(_workbench().queue_outreach(draft_id, account_id=account_id,
                      recipient=recipient, expected_hash=content_hash), ensure_ascii=False, indent=2)


def trade_workbench_approve_quote(quote_draft_id: str, content_hash: str) -> str:
    """人工批准报价草稿；不发布、不发送。"""
    return json.dumps(_workbench().approve_quote(quote_draft_id,
                      expected_hash=content_hash), ensure_ascii=False, indent=2)


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = load_business_config()
    init_db()
    from agent.business.seed_data import seed_database
    if seed_database():
        print("[QuoteBusiness] 种子数据已导入")
    print(f"[QuoteBusiness] 数据库就绪: {cfg.mysql_database}@{cfg.mysql_host}:{cfg.mysql_port}", file=sys.stderr)
    print(f"[QuoteBusiness] LLM: {cfg.llm_model}")
    print(f"[QuoteBusiness] 后端: {cfg.database_backend}", file=sys.stderr)
    print(f"[QuoteBusiness] 汇率: {len(cfg.exchange_rates)} 种货币", file=sys.stderr)
    try:
        mcp.run(transport="stdio")
    finally:
        close_all()
        # stdout is reserved for MCP stdio frames; do not write protocol noise.
