---
name: product-loader
description: Load authoritative product, SKU, specification, MOQ, packaging, certification, lead-time, and pricing context.
---
# Product Loader

Use NanoClaw's business database first and optional YAML/JSON/CSV/XLSX imports
second.  Never invent a product claim or reuse one SKU's packaging for another.
Return `company`, `products`, `pricing_rules`, `missing_fields`, `source_kind`,
and `safe_to_quote`.  Current seed products are `demo_only`.  `safe_to_quote`
is true only when exact SKU, quantity, approved unit price, currency, incoterm,
payment terms, validity, product size, packing size, MOQ, and lead time exist.

