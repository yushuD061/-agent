"""
报价单版本管理工具

生成报价单版本，带版本号控制。
每次修改生成新版本，历史版本可追溯。
"""

import json

from agent.business.database import get_rfq, create_quote, add_quote_version
from agent.models.schemas import QuoteItem, QuoteVersion


def create_quote_version_impl(
    rfq_id: int,
    customer_name: str,
    customer_company: str,
    country: str,
    items: list[dict],
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

    每次调用创建一个新报价单（rfq_id 相同可重复创建，用于多版对比）。
    如需历史版本追踪，可由 LLM 或业务流程控制。

    Args:
        rfq_id: 询盘 ID
        customer_name: 客户名
        customer_company: 客户公司
        country: 国家
        items: 报价项列表，每项含 sku/product_name_en/quantity/unit_price_usd/total_price_usd
        subtotal_usd: 小计
        discount_percent: 折扣百分比
        discount_amount: 折扣金额
        packaging_cost_usd: 包装费
        freight_cost_usd: 运费
        total_usd: 总计
        validity_days: 有效期天数
        valid_until: 有效期截止
        payment_terms: 支付条款
        delivery_term: 贸易条款
        remarks_cn: 中文备注
        remarks_en: 英文备注
        risk_notes: 风险提示
        exchange_rate_note: 汇率说明

    Returns:
        报价单结果
    """
    # 验证询盘存在
    rfq = get_rfq(rfq_id)
    if not rfq:
        return json.dumps({"error": f"询盘 #{rfq_id} 不存在"}, ensure_ascii=False, indent=2)

    # 构建报价项
    quote_items = []
    for i, item_data in enumerate(items):
        qi = QuoteItem(
            product_sku=item_data.get("sku", ""),
            product_name_en=item_data.get("product_name_en", ""),
            quantity=int(item_data.get("quantity", 0)),
            unit_price_usd=float(item_data.get("unit_price_usd", 0)),
            total_price_usd=float(item_data.get("total_price_usd", 0)),
            moq_note=item_data.get("moq_note", ""),
        )
        quote_items.append(qi)

    # 创建报价单
    rfq_preview = rfq.raw_text[:100] + "..." if len(rfq.raw_text) > 100 else rfq.raw_text
    quote_id = create_quote(rfq_id, customer_name, customer_company, country, rfq_preview)

    version = QuoteVersion(
        version=1,
        items=quote_items,
        subtotal_usd=subtotal_usd,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        packaging_cost_usd=packaging_cost_usd,
        freight_cost_usd=total_usd*0.2,
        total_usd=total_usd,
        validity_days=validity_days,
        valid_until=valid_until,
        payment_terms=payment_terms,
        delivery_term=delivery_term,
        remarks_cn=remarks_cn,
        remarks_en=remarks_en,
        exchange_rate_note=exchange_rate_note,
    )
    add_quote_version(quote_id, version)

    # 保存风险提示
    if risk_notes:
        from agent.business.database import tx
        with tx() as cur:
            cur.execute("UPDATE quotes SET risk_notes = ? WHERE id = ?", (risk_notes, quote_id))

    result = {
        "quote_id": quote_id,
        "new_version": 1,
        "status": "created",
        "message": f"报价单 #{quote_id} (v1) 已创建",
    }
    return json.dumps(result, ensure_ascii=False, indent=2)
