"""
报价计算工具

纯确定性计算，不调用 LLM：
- 单价 × 数量 = 小计
- 批量折扣阶梯
- 包装费、运费
- 汇率转换
- 有效期

验收要求：报价计算正确率必须 100%
"""

import json
from datetime import datetime, timedelta
from typing import Optional

from agent.business.config import load_business_config
from agent.business.database import get_product_by_sku
from agent.models.schemas import QuoteItem, QuoteVersion, date_iso


def _calc_discount(quantity: int, unit_price: float) -> tuple[float, float]:
    """计算批量折扣，返回 (折扣百分比, 折扣金额)

    阶梯规则（可配置，当前硬编码为示例值）：
    - 1-99:   无折扣
    - 100-499:  3%
    - 500-999:  5%
    - 1000+:    8%
    """
    if quantity >= 1000:
        pct = 8.0
    elif quantity >= 500:
        pct = 5.0
    elif quantity >= 100:
        pct = 3.0
    else:
        pct = 0.0

    amount = round(unit_price * quantity * pct / 100, 2)
    return pct, amount


def calculate_quote_impl(
    sku: str,
    quantity: int,
    unit_price_usd: Optional[float] = None,
    markup_percent: Optional[float] = None,
    delivery_term: str = "FOB",
    destination_country: str = "",
    packaging_cost_usd: float = 0.0,
    freight_cost_usd: float = 0.0,
    validity_days: Optional[int] = None,
    target_currency: str = "USD",
    discount_percent_override: Optional[float] = None,
) -> str:
    """计算报价

    Args:
        sku: 产品 SKU
        quantity: 数量
        unit_price_usd: 美元单价（不传则用产品库价格）
        markup_percent: 加价百分比（不传则用默认值）
        delivery_term: 贸易条款 (FOB/CIF/EXW/DDP)
        destination_country: 目的国
        packaging_cost_usd: 包装费（美元）
        freight_cost_usd: 运费（美元）
        validity_days: 有效期天数
        target_currency: 目标货币
        discount_percent_override: 折扣百分比覆盖

    Returns:
        报价计算结果
    """
    cfg = load_business_config()

    # 1. 获取产品信息
    product = get_product_by_sku(sku)
    if not product:
        return json.dumps({"error": f"SKU '{sku}' 不存在"}, ensure_ascii=False, indent=2)

    if quantity < product.moq:
        return json.dumps({
            "error": f"数量 {quantity} 小于最小起订量 {product.moq}",
            "sku": sku,
            "moq": product.moq,
        }, ensure_ascii=False, indent=2)

    # 2. 确定单价
    base_price = unit_price_usd if unit_price_usd is not None else product.price_usd
    markup = markup_percent if markup_percent is not None else cfg.default_markup_percent
    final_unit_price = round(base_price * (1 + markup / 100), 2)

    # 3. 小计
    subtotal = round(final_unit_price * quantity, 2)

    # 4. 折扣
    if discount_percent_override is not None:
        discount_pct = discount_percent_override
        discount_amt = round(subtotal * discount_pct / 100, 2)
    else:
        discount_pct, discount_amt = _calc_discount(quantity, final_unit_price)

    # 5. 总价
    total_before_freight = subtotal - discount_amt
    total_usd = round(total_before_freight + packaging_cost_usd + freight_cost_usd, 2)

    # 6. 有效期
    vd = validity_days if validity_days is not None else cfg.default_validity_days
    valid_until = date_iso(days_offset=vd)

    # 7. 汇率转换（如果目标货币不是 USD）
    exchange_rate_note = ""
    total_target = total_usd
    if target_currency.upper() != "USD":
        rate = cfg.exchange_rates.get(target_currency.upper(), 0)
        if rate > 0:
            total_target = round(total_usd * (1 / cfg.exchange_rates.get("USD", 1)) * rate, 2) if target_currency.upper() != "USD" else total_usd
            # 简化：直接用配置中的汇率
            usd_to_target = cfg.exchange_rates.get(target_currency.upper())
            if usd_to_target:
                total_target = round(total_usd * usd_to_target, 2)
                exchange_rate_note = f"1 USD = {usd_to_target} {target_currency.upper()}"
        else:
            exchange_rate_note = f"未配置 {target_currency.upper()} 汇率，使用 USD 计价"

    # 8. 构建结果
    item = QuoteItem(
        product_sku=sku,
        product_name_en=product.name_en,
        quantity=quantity,
        unit_price_usd=final_unit_price,
        total_price_usd=subtotal,
        moq_note=f"MOQ: {product.moq}",
    )

    version = QuoteVersion(
        version=1,
        items=[item],
        subtotal_usd=subtotal,
        discount_percent=discount_pct,
        discount_amount=discount_amt,
        packaging_cost_usd=packaging_cost_usd,
        freight_cost_usd=freight_cost_usd,
        total_usd=total_usd,
        exchange_rate_note=exchange_rate_note,
        validity_days=vd,
        valid_until=valid_until,
        delivery_term=delivery_term,
    )

    result = {
        "sku": sku,
        "product_name": product.name_en,
        "base_price_usd": base_price,
        "markup_percent": markup,
        "unit_price_usd": final_unit_price,
        "quantity": quantity,
        "subtotal_usd": subtotal,
        "discount_percent": discount_pct,
        "discount_amount_usd": discount_amt,
        "packaging_cost_usd": packaging_cost_usd,
        "freight_cost_usd": freight_cost_usd,
        "total_usd": total_usd,
        "total_target_currency": target_currency.upper(),
        "total_target_amount": total_target,
        "exchange_rate_note": exchange_rate_note,
        "valid_until": valid_until,
        "delivery_term": delivery_term,
        "moq": product.moq,
        "unit": product.unit,
        "_calculation_note": "以上金额由确定性算法计算，未调用 LLM",
    }

    return json.dumps(result, ensure_ascii=False, indent=2)
