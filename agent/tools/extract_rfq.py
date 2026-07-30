"""
询盘字段抽取工具

使用 LLM 从英文询盘文本中提取结构化字段。
缺字段时标记为"待确认"而非自行补全（防幻觉）。
"""

import json

from agent.models.schemas import RfqFieldExtraction
from agent.business.database import create_rfq, update_rfq_extraction
from agent.business.rfq_extractor import extract_rfq_fields

async def extract_rfq_impl(session_key: str, raw_text: str) -> str:
    """抽取询盘字段

    Args:
        session_key: 会话标识
        raw_text: 询盘原始文本

    Returns:
        抽取结果 JSON 字符串
    """
    # 1. 创建询盘记录
    rfq_id = create_rfq(session_key, raw_text)

    try:
        data = await extract_rfq_fields(raw_text, {"source": "chat"})
        first_item = data["items"][0]
        customer = data["customer"]
        trade = data["trade_term"]
        deadline = data["delivery_deadline"]
        extracted = RfqFieldExtraction(
            customer_name=customer["name"].get("value") or "",
            customer_company=customer["company"].get("value") or "",
            customer_country=data["country"].get("value") or "",
            product_description=first_item["product"].get("value") or "",
            quantity=int(first_item["quantity"].get("value") or 0),
            unit=first_item["quantity"].get("unit") or "",
            delivery_term=" ".join(filter(None, (trade.get("incoterm"), trade.get("named_place")))),
            delivery_deadline=deadline.get("raw") or "",
            missing_fields=data.get("missing_fields", []),
        )

        # 3. 更新数据库
        update_rfq_extraction(rfq_id, extracted)

        # 4. 返回结果
        result = {"rfq_id": rfq_id, "schema_version": "rfq-v2", "extraction": data,
                  "_warning": "未确认的字段不能进入自动报价，请人工核实"}

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": "RFQ extraction failed", "error_code": type(e).__name__, "rfq_id": rfq_id},
                          ensure_ascii=False)
