"""
库存校验工具

校验指定 SKU 的数量是否满足库存和最小起订量。
"""

import json

from agent.business.database import check_inventory


def check_inventory_impl(sku: str, quantity: int) -> str:
    """校验库存

    Args:
        sku: 产品 SKU
        quantity: 需求数量

    Returns:
        校验结果
    """
    result = check_inventory(sku, quantity)
    return json.dumps(result, ensure_ascii=False, indent=2)
