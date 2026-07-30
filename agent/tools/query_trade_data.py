"""Fixed, allowlisted operational analytics tool for the M4 MVP."""

import json
from typing import Any

from agent.business.analytics import AnalyticsError, execute_fixed_query, list_query_contracts


def query_trade_data_impl(query_code: str = "", filters: dict[str, Any] | str | None = None,
                          list_contracts: bool = False) -> str:
    if list_contracts:
        return json.dumps({"contracts": list_query_contracts(), "nl2sql_enabled": False}, ensure_ascii=False)
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except json.JSONDecodeError:
            return json.dumps({"error": "invalid_filters", "error_code": "invalid_filters"})
    if filters is not None and not isinstance(filters, dict):
        return json.dumps({"error": "invalid_filters", "error_code": "invalid_filters"})
    try:
        return json.dumps(execute_fixed_query(query_code, filters), ensure_ascii=False, default=str)
    except AnalyticsError as exc:
        return json.dumps({"error": exc.code, "error_code": exc.code}, ensure_ascii=False)
