"""
初始化演示数据

首次运行时填充产品库、汇率等基础数据。
产品数据为外贸常见品类示例，仅供演示使用。
"""

import json
import os

from agent.business.database import tx, init_db, get_connection
from agent.models.schemas import now_iso
from agent.business.config import load_business_config


SEED_PRODUCTS = [
    # 电子配件
    {"sku": "BL-1001", "name_cn": "蓝牙耳机", "name_en": "Bluetooth Earphone TWS", "category": "electronics", "specification": "V5.3, 30h battery, IPX5", "unit": "pairs", "moq": 100, "price_usd": 8.50, "weight_kg": 0.08, "package_info": "Blister pack, 100pcs/ctn, 0.3cbm", "inventory": 5000, "lead_time_days": 15},
    {"sku": "SP-2001", "name_cn": "便携蓝牙音箱", "name_en": "Portable Bluetooth Speaker", "category": "electronics", "specification": "10W, IPX7, 12h battery", "unit": "pcs", "moq": 50, "price_usd": 12.00, "weight_kg": 0.35, "package_info": "Color box, 50pcs/ctn, 0.5cbm", "inventory": 2000, "lead_time_days": 20},
    {"sku": "CB-3001", "name_cn": "USB-C 数据线", "name_en": "USB-C Cable 1m", "category": "electronics", "specification": "PD 100W, USB 3.1, braided", "unit": "pcs", "moq": 500, "price_usd": 1.80, "weight_kg": 0.03, "package_info": "Poly bag, 500pcs/ctn, 0.2cbm", "inventory": 20000, "lead_time_days": 10},
    # 家居用品
    {"sku": "KM-4001", "name_cn": "不锈钢厨刀套装", "name_en": "Stainless Steel Kitchen Knife Set", "category": "kitchen", "specification": "5-piece set, German steel", "unit": "set", "moq": 50, "price_usd": 25.00, "weight_kg": 1.20, "package_info": "Gift box, 10sets/ctn, 0.4cbm", "inventory": 800, "lead_time_days": 25},
    {"sku": "KM-4002", "name_cn": "硅胶锅铲", "name_en": "Silicone Spatula Set", "category": "kitchen", "specification": "3-piece, heat resistant 230C", "unit": "set", "moq": 200, "price_usd": 3.50, "weight_kg": 0.15, "package_info": "Hanger card, 200pcs/ctn, 0.3cbm", "inventory": 5000, "lead_time_days": 15},
    # 服装配件
    {"sku": "BG-5001", "name_cn": "帆布背包", "name_en": "Canvas Backpack", "category": "bags", "specification": "40L, water-resistant, multi-pocket", "unit": "pcs", "moq": 100, "price_usd": 15.00, "weight_kg": 0.50, "package_info": "Poly bag, 50pcs/ctn, 0.6cbm", "inventory": 1500, "lead_time_days": 20},
    {"sku": "BG-5002", "name_cn": "旅行洗漱包", "name_en": "Travel Toiletry Bag", "category": "bags", "specification": "Waterproof, hanging, 3L", "unit": "pcs", "moq": 200, "price_usd": 4.50, "weight_kg": 0.12, "package_info": "Poly bag, 200pcs/ctn, 0.25cbm", "inventory": 8000, "lead_time_days": 12},
    # 玩具
    {"sku": "TY-6001", "name_cn": "遥控赛车", "name_en": "RC Racing Car", "category": "toys", "specification": "1:16 scale, 2.4G, rechargeable", "unit": "pcs", "moq": 100, "price_usd": 18.00, "weight_kg": 0.60, "package_info": "Display box, 24pcs/ctn, 0.5cbm", "inventory": 1200, "lead_time_days": 20},
    {"sku": "TY-6002", "name_cn": "积木套装", "name_en": "Building Blocks 500pcs", "category": "toys", "specification": "ABS, compatible with Lego, 500pcs", "unit": "set", "moq": 200, "price_usd": 9.00, "weight_kg": 0.80, "package_info": "Storage box, 20sets/ctn, 0.6cbm", "inventory": 3000, "lead_time_days": 15},
    # 促销品
    {"sku": "PM-7001", "name_cn": "定制马克杯", "name_en": "Custom Ceramic Mug 350ml", "category": "promotional", "specification": "Ceramic, 350ml, logo printing available", "unit": "pcs", "moq": 500, "price_usd": 1.50, "weight_kg": 0.35, "package_info": "White box, 50pcs/ctn, 0.3cbm", "inventory": 10000, "lead_time_days": 25},
    {"sku": "PM-7002", "name_cn": "定制圆珠笔", "name_en": "Custom Ballpoint Pen", "category": "promotional", "specification": "Metal body, logo printing, blue ink", "unit": "pcs", "moq": 1000, "price_usd": 0.35, "weight_kg": 0.01, "package_info": "Poly bag, 1000pcs/ctn, 0.2cbm", "inventory": 50000, "lead_time_days": 20},
]

SEED_EXCHANGE_RATES = {
    "USD": 1.0,
    "CNY": 7.25,
    "EUR": 0.92,
    "GBP": 0.79,
    "JPY": 149.50,
    "AUD": 1.53,
    "CAD": 1.36,
    "KRW": 1320.0,
    "INR": 83.0,
    "SGD": 1.34,
}


def seed_database(force: bool = False) -> bool:
    """初始化种子数据，已有数据时跳过

    Args:
        force: 是否强制重新导入

    Returns:
        bool: 是否导入了数据
    """
    init_db()
    ts = now_iso()
    imported = False
    with tx() as cur:
        backend = load_business_config().database_backend
        if backend == "sqlite":
            for p in SEED_PRODUCTS:
                cur.execute("INSERT OR IGNORE INTO products (sku,name_cn,name_en,category,specification,unit,moq,price_usd,inventory,lead_time_days,active) VALUES (?,?,?,?,?,?,?,?,?,?,1)", (p["sku"],p["name_cn"],p["name_en"],p["category"],p["specification"],p["unit"],p["moq"],p["price_usd"],p["inventory"],p["lead_time_days"]))
            for currency, rate in SEED_EXCHANGE_RATES.items():
                if currency != "USD": cur.execute("INSERT OR REPLACE INTO exchange_rates(from_currency,to_currency,rate) VALUES(?,?,?)", ("USD",currency,rate)); cur.execute("INSERT OR REPLACE INTO exchange_rates(from_currency,to_currency,rate) VALUES(?,?,?)", (currency,"USD",1.0/rate))
            return True
        for p in SEED_PRODUCTS:
            if force:
                cur.execute("DELETE FROM ops_product WHERE sku=%s", (p["sku"],))
            cur.execute("SELECT product_key FROM ops_product WHERE sku=%s", (p["sku"],))
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO ops_product (sku,name_cn,name_en,category_code,specification_text,quantity_unit,moq,lead_time_days,sale_status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')",
                (p["sku"], p["name_cn"], p["name_en"], p["category"], p["specification"], p["unit"], p["moq"], p["lead_time_days"]),
            )
            cur.execute(
                "INSERT INTO ops_product_price_rule (sku,min_qty,max_qty,unit_price,currency_code,discount_rate,valid_from,approval_level) VALUES (%s,1,NULL,%s,'USD',0,%s,'standard')",
                (p["sku"], p["price_usd"], ts),
            )
            cur.execute(
                "INSERT INTO ops_inventory_snapshot (snapshot_at,location_code,sku,on_hand_qty,reserved_qty,available_qty) VALUES (%s,'DEFAULT',%s,%s,0,%s)",
                (ts, p["sku"], p["inventory"], p["inventory"]),
            )
            imported = True
        for currency, rate in SEED_EXCHANGE_RATES.items():
            if currency == "USD":
                continue
            cur.execute("INSERT INTO ops_fx_rate_snapshot (from_currency,to_currency,rate,source_code,quoted_at) VALUES ('USD',%s,%s,'seed',%s)", (currency, rate, ts))
            cur.execute("INSERT INTO ops_fx_rate_snapshot (from_currency,to_currency,rate,source_code,quoted_at) VALUES (%s,'USD',%s,'seed',%s)", (currency, 1.0 / rate, ts))
    return imported


if __name__ == "__main__":
    imported = seed_database()
    if imported:
        print(f"种子数据已导入: {len(SEED_PRODUCTS)} 个产品, {len(SEED_EXCHANGE_RATES)} 种货币")
    else:
        print("数据库已有数据，跳过导入")
