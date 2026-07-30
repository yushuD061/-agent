"""
产品库搜索工具

根据关键词或分类在产品库中匹配 SKU。
返回 Top-N 候选及匹配依据。
"""

import json

from agent.business.database import list_products, get_product_by_sku


def search_product_catalog_impl(
    keyword: str = "",
    category: str = "",
    sku: str = "",
    limit: int = 5,
) -> str:
    """搜索产品库

    Args:
        keyword: 搜索关键词（在产品名、描述、规格中匹配）
        category: 产品分类过滤
        sku: 直接按 SKU 查询
        limit: 最多返回结果数

    Returns:
        搜索结果 JSON
    """
    results = []

    # 精确 SKU 匹配
    if sku:
        product = get_product_by_sku(sku)
        if product:
            results.append({
                "sku": product.sku,
                "name_en": product.name_en,
                "name_cn": product.name_cn,
                "category": product.category,
                "specification": product.specification,
                "unit": product.unit,
                "price_usd": product.price_usd,
                "moq": product.moq,
                "inventory": product.inventory,
                "lead_time_days": product.lead_time_days,
                "match_type": "exact_sku",
                "package_info": product.package_info,
            })
            return json.dumps({"results": results, "total": 1, "match_method": "exact_sku"}, ensure_ascii=False, indent=2)

    # 关键词模糊匹配
    products = list_products(category=category, keyword=keyword, limit=limit)

    for p in products:
        results.append({
            "sku": p.sku,
            "name_en": p.name_en,
            "name_cn": p.name_cn,
            "category": p.category,
            "specification": p.specification,
            "unit": p.unit,
            "price_usd": p.price_usd,
            "moq": p.moq,
            "inventory": p.inventory,
            "lead_time_days": p.lead_time_days,
            "match_type": "keyword",
            "package_info": p.package_info,
        })

    if not results:
        return json.dumps({"results": [], "total": 0, "message": "未找到匹配产品，请尝试其他关键词"}, ensure_ascii=False, indent=2)

    return json.dumps({"results": results, "total": len(results), "match_method": "keyword"}, ensure_ascii=False, indent=2)
