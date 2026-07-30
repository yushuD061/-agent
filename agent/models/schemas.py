"""
外贸业务数据模型

所有业务对象用 dataclass 定义，与数据库表结构一一对应。
日期字段统一用 ISO 格式字符串，避免跨环境时区问题。
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


# ── 产品 ──────────────────────────────────────────────────

@dataclass
class Product:
    """产品"""
    id: int = 0
    sku: str = ""
    name_cn: str = ""
    name_en: str = ""
    category: str = ""
    specification: str = ""
    unit: str = "pcs"
    moq: int = 1                  # 最小起订量
    price_usd: float = 0.0       # 美元单价 (FOB)
    weight_kg: float = 0.0       # 单件重量
    package_info: str = ""        # 包装信息
    inventory: int = 0            # 库存数量
    lead_time_days: int = 15     # 生产周期
    active: bool = True
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Customer:
    """客户"""
    id: int = 0
    name: str = ""
    company: str = ""
    country: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""


# ── 询盘 ──────────────────────────────────────────────────

@dataclass
class RfqFieldExtraction:
    """询盘字段抽取结果"""
    customer_name: str = ""
    customer_company: str = ""
    customer_country: str = ""
    product_description: str = ""
    quantity: int = 0
    unit: str = "pcs"
    target_price: str = ""        # 客户目标价（原始文本）
    delivery_term: str = ""       # 贸易条款 FOB/CIF/EXW/DDP
    delivery_deadline: str = ""   # 交期
    payment_term: str = ""        # 支付方式
    special_requirements: str = ""
    missing_fields: list[str] = field(default_factory=list)


@dataclass
class RfqRequest:
    """询盘记录"""
    id: int = 0
    session_key: str = ""
    raw_text: str = ""
    extracted: Optional[RfqFieldExtraction] = None
    status: str = "pending"       # pending / extracting / extracted / quoting / quoted / approved / rejected
    customer_id: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""


# ── 报价 ──────────────────────────────────────────────────

@dataclass
class QuoteItem:
    """报价单项"""
    product_sku: str = ""
    product_name_en: str = ""
    quantity: int = 0
    unit_price_usd: float = 0.0
    total_price_usd: float = 0.0
    moq_note: str = ""


@dataclass
class QuoteVersion:
    """报价版本"""
    version: int = 1
    items: list[QuoteItem] = field(default_factory=list)
    subtotal_usd: float = 0.0
    discount_percent: float = 0.0
    discount_amount: float = 0.0
    packaging_cost_usd: float = 0.0
    freight_cost_usd: float = 0.0
    total_usd: float = 0.0
    exchange_rate_note: str = ""
    validity_days: int = 15
    valid_until: str = ""
    payment_terms: str = ""
    delivery_term: str = ""
    remarks_cn: str = ""
    remarks_en: str = ""
    created_by: str = "agent"
    created_at: str = ""


@dataclass
class Quote:
    """报价单"""
    id: int = 0
    rfq_id: int = 0
    rfq_text_preview: str = ""
    customer_name: str = ""
    customer_company: str = ""
    country: str = ""
    status: str = "draft"         # draft / pending_approval / approved / rejected / sent
    current_version: int = 1
    versions: list[QuoteVersion] = field(default_factory=list)
    risk_notes: str = ""
    created_at: str = ""
    updated_at: str = ""


# ── 跟进任务 ──────────────────────────────────────────────

@dataclass
class FollowupTask:
    """跟进任务"""
    id: int = 0
    quote_id: int = 0
    task_type: str = "follow_up"  # follow_up / approval_reminder / customer_reply
    title: str = ""
    description: str = ""
    due_at: str = ""
    status: str = "pending"       # pending / completed / cancelled
    customer_reply: str = ""
    created_at: str = ""
    completed_at: str = ""


# ── 审批记录 ──────────────────────────────────────────────

@dataclass
class ApprovalRecord:
    """审批记录"""
    id: int = 0
    quote_id: int = 0
    version: int = 1
    status: str = "pending"       # pending / approved / rejected
    reviewer: str = ""
    comment: str = ""
    created_at: str = ""
    decided_at: str = ""


# ── 汇率 ──────────────────────────────────────────────────

@dataclass
class ExchangeRate:
    """汇率"""
    from_currency: str = "USD"
    to_currency: str = "CNY"
    rate: float = 1.0
    updated_at: str = ""


# ── 工具函数 ──────────────────────────────────────────────

def now_iso() -> str:
    """返回当前时间的 ISO 格式字符串"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def date_iso(days_offset: int = 0) -> str:
    """返回日期 ISO 字符串，可选偏移天数"""
    from datetime import timedelta
    d = datetime.now() + timedelta(days=days_offset)
    return d.strftime("%Y-%m-%d")
