"""
跟进任务创建工具

为报价单创建跟进任务，支持催批、客户回复跟进等。
"""

import json

from agent.business.database import create_followup, get_quote
from agent.models.schemas import date_iso


def create_followup_task_impl(
    quote_id: int,
    task_type: str = "follow_up",
    title: str = "",
    description: str = "",
    due_days: int = 3,
) -> str:
    """创建跟进任务

    Args:
        quote_id: 报价单 ID
        task_type: 任务类型 (follow_up / approval_reminder / customer_reply)
        title: 任务标题
        description: 任务描述
        due_days: 截止天数（从今天起）

    Returns:
        创建结果
    """
    # 验证报价单存在
    quote = get_quote(quote_id)
    if not quote:
        return json.dumps({"error": f"报价单 #{quote_id} 不存在"}, ensure_ascii=False, indent=2)

    if not title:
        type_labels = {
            "follow_up": "跟进客户",
            "approval_reminder": "审批提醒",
            "customer_reply": "客户回复处理",
        }
        title = type_labels.get(task_type, "跟进任务")

    due_at = date_iso(days=due_days)
    task_id = create_followup(quote.rfq_id, quote_id, task_type, title, description, due_at)

    result = {
        "task_id": task_id,
        "quote_id": quote_id,
        "title": title,
        "due_at": due_at,
        "status": "pending",
        "message": f"跟进任务 '{title}' 已创建，截止日期 {due_at}",
    }
    return json.dumps(result, ensure_ascii=False, indent=2)
